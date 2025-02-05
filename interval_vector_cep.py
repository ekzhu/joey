from utils import * 
import sqlite3

def vector_interval_cep(batch, events, time_col, max_span, by = None, event_udfs = {}):
    
    assert type(batch) == polars.DataFrame, "batch must be a polars DataFrame"
    if by is None:
        assert batch[time_col].is_sorted(), "batch must be sorted by time_col"
    else:
        assert by in batch.columns

    batch, event_names, rename_dicts, event_predicates, event_indices, event_independent_columns, event_required_columns , event_udfs, intervals = preprocess_2(batch, events, time_col, by, event_udfs, max_span)
    # this hack needs to be corrected, this basically makes all the {event_name}_{col_name} col names just {col_name} for the current event
    for i in range(1, len(event_names)):
        for col in event_independent_columns[event_names[i]]:
            event_predicates[i] = event_predicates[i].replace(event_names[i] + "_" + col, col)

    total_events = len(events)

    if event_indices[event_names[0]] == None:
        print("vectored cep is for things with first event filter, likely will be slow")

    con = sqlite3.connect(":memory:")
    cur = con.cursor()

    for i in range(1, total_events):
        if event_indices[event_names[i]] == None:
            batch = batch.with_columns(polars.lit(True).alias("__possible_{}__".format(event_names[i])))
            continue
        event_df = polars.from_dict({"event_nrs": list(event_indices[event_names[i]]) })\
            .with_columns([polars.col("event_nrs").cast(polars.UInt32()),
                polars.lit(True).alias("__possible_{}__".format(event_names[i]))])
        batch = batch.join(event_df, left_on = "__row_count__", right_on = "event_nrs", how = "left")

    partitioned = batch.partition_by(by, as_dict = True) if by is not None else {"dummy":batch}

    matched_ends = []
    total_exec_times = []
    overhead = 0

    matched_events = []

    length_dicts = {event_name: [] for event_name in event_names}

    cur = cur.execute("create table frame({})".format(", ".join(batch.columns)))
    cur = cur.execute("CREATE INDEX idx ON frame(__row_count__);")
    row_count_idx = batch.columns.index("__row_count__")

    results = intervals.partition_by(by, as_dict = True) if by is not None else {"dummy": intervals}

    for key in tqdm(results) if by is not None else results:
        partition = partitioned[key]
        result = results[key]

        # this is expensive!
        partition_tuples = partition.rows()
        s = ",".join(["?"] * len(partition_tuples[0]))
        cur = cur.executemany("insert into frame values({})".format(s), partition_tuples)
        
        start_row_count = partition_tuples[0][row_count_idx]
        
        for bound in (tqdm(result.to_dicts()) if by is None else result.to_dicts()):

            start_nr = bound["__arc__"]
            end_nr = bound["__crc__"]

            # match recognize default is one pattern per start. we can change it to one pattern per end too
            # if end_nr in matched_ends:

            if start_nr in matched_ends or end_nr <= start_nr:
                continue
            fate = {event_names[0] + "_" + col : partition_tuples[start_nr - start_row_count][i] for i, col in enumerate(partition.columns)}
            stack = deque([(0, [fate], [start_nr])])
            
            while stack:
                marker, path, matched_event = stack.popleft()
                next_event_name = event_names[len(path)]
                next_event_filter = event_predicates[len(path)]
                
                # now fill in the next_event_filter based on the previous path
                for fate in path:
                    for fixed_col in fate:
                        next_event_filter = next_event_filter.replace(fixed_col, str(fate[fixed_col]))
                
                startt = time.time()
                if next_event_name == event_names[-1]:
                    query = "select * from frame where __possible_{}__ and {} and __row_count__ > {} and __row_count__ <= {} limit 1".format(next_event_name, next_event_filter, start_nr + marker,end_nr )
                else:
                    query = "select * from frame where __possible_{}__ and {} and __row_count__ > {} and __row_count__ <= {}".format(next_event_name, next_event_filter, start_nr + marker,end_nr )

                matched = cur.execute(query).fetchall()
                total_exec_times.append(time.time() - startt)
                length_dicts[next_event_name].append(end_nr - start_nr - marker)
                
                if len(matched) > 0:
                    if next_event_name == event_names[-1]:
                        matched_ends.append(start_nr)
                        matched_events.append(matched_event + [matched[0][row_count_idx]])
                        break
                    else:
                        for matched_row in matched[::-1]:
                            my_fate = {next_event_name + "_" + partition.columns[i] : matched_row[i] for i in range(len(partition.columns))}
                            stack.appendleft((matched_row[row_count_idx] - start_nr, path + [my_fate], matched_event + [matched_row[row_count_idx]]))

        cur = cur.execute("delete from frame")    

    # now create a dataframe from the matched events, first flatten matched_events into a flat list
    if len(matched_events) > 0:
        matched_events = [item for sublist in matched_events for item in sublist]
        matched_events = batch[matched_events].select(["__row_count__", time_col, by]) if by is not None else batch[matched_events].select(["__row_count__", time_col])
        
        events = [matched_events[i::total_events] for i in range(total_events)]
        for i in range(total_events):
            if i != 0 and by is not None:
                events[i] = events[i].drop(by)
            events[i] = events[i].rename({"__row_count__" : event_names[i] + "___row_count__", time_col : event_names[i] + "_" + time_col})
        matched_events = polars.concat(events, how = 'horizontal')

        print("TIME SPENT IN FILTER {} {} ".format(sum(total_exec_times), len(total_exec_times)))
        for key in length_dicts:
            print(key, len(length_dicts[key]), np.mean(length_dicts[key]))
        
        print("TOTAL FILTER EVENTS: ", sum([len(length_dicts[key]) for key in length_dicts]))
        print("TOTAL FILTERED ROWS: ", sum([np.sum(length_dicts[key]) for key in length_dicts]))
        print("OVERHEAD", overhead)
        return matched_events
    else:
        return None