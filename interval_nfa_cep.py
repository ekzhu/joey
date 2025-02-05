from utils import * 
import sqlite3
import duckdb
import pickle

def nfa_interval_cep(batch, events, time_col, max_span, by = None, event_udfs = {}):
    
    assert type(batch) == polars.DataFrame, "batch must be a polars DataFrame"
    if by is None:
        assert batch[time_col].is_sorted(), "batch must be sorted by time_col"
    else:
        assert by in batch.columns

    batch, event_names, rename_dicts, event_predicates, event_indices, event_independent_columns, event_required_columns , event_udfs, intervals = preprocess_2(batch, events, time_col, by, event_udfs, max_span)

    total_events = len(events)

    assert event_indices[event_names[0]] != None, "this is for things with first event filter"

    matched_events = []

    total_filter_time = 0
    total_filter_times = {event_name: [] for event_name in event_names}
    total_other_time = 0
    counter = 0

    con = sqlite3.connect(":memory:")
    cur = con.cursor()

    # cur = duckdb.connect()

    partitioned = batch.partition_by(by, as_dict = True) if by is not None else {"dummy":batch}
    length_dicts = {event_name: [] for event_name in event_names}

    current_cols = []
    for event in range(total_events - 1):
        event_name = event_names[event]
        current_cols += [event_name + "___row_count__"] + [event_name + "_" + k for k in event_required_columns[event_name]]
        cur = cur.execute("create table matched_sequences_{}({})".format(event, ", ".join(current_cols)))

    # get the indices of row count cols in current_cols
    row_count_idx = [i for i in range(len(current_cols)) if "__row_count__" in current_cols[i]]

    results = intervals.partition_by(by, as_dict = True) if by is not None else {"dummy": intervals}

    for key in tqdm(results) if by is not None else results:
        partition = partitioned[key]
        result = results[key]

        # this is expensive!
        partition_rows = partition.to_dicts()
        
        start_row_count = partition["__row_count__"][0]
        for bound in (tqdm(result.to_dicts()) if by is None else result.to_dicts()):

            start_nr = bound["__arc__"]
            end_nr = bound["__crc__"]

            interval = partition_rows[start_nr - start_row_count: end_nr + 1 - start_row_count] #.to_arrow()                        
            val = ",".join([str(interval[0]["__row_count__"])] + [str(interval[0][k]) for k in event_required_columns[event_names[0]]])
            cur = cur.execute("insert into matched_sequences_0 values ({})".format(val))

            empty = {seq_len: True for seq_len in range(1, total_events)}
            empty[0] = False

            for row in range(1, len(interval)):
                global_row_count = interval[row]["__row_count__"]
                this_row_can_be = [i for i in range(1, total_events) if event_indices[event_names[i]] is None or global_row_count in event_indices[event_names[i]]]
                early_exit = False
                
                for seq_len in sorted(this_row_can_be)[::-1]:

                    if empty[seq_len - 1]:
                        continue
                    
                    # evaluate the predicate against matched_sequences[seq_len - 1]
                    predicate = event_predicates[seq_len]
                    assert predicate is not None
                    for col in event_independent_columns[event_names[seq_len]]:
                        predicate = predicate.replace(event_names[seq_len] + "_" + col, str(interval[row][col]))
                    
                    start = time.time()
                    
                    matched = cur.execute("select * from matched_sequences_{} where {}".format(seq_len - 1, predicate)).fetchall()

                    length_dicts[event_names[seq_len]].append(1)
                    total_filter_time += time.time() - start
                    total_filter_times[event_names[seq_len]].append(time.time() - start)
                    
                    # now horizontally concatenate your table against the matched
                    if len(matched) > 0:
                       
                        if seq_len == total_events - 1:
                            row_counts = [matched[0][i] for i in row_count_idx] + [interval[row]["__row_count__"]]
                            matched_events.append(row_counts)
                            early_exit = True
                            break
                        else:
                            val = tuple([interval[row]["__row_count__"]] + [interval[row][k] for k in event_required_columns[event_names[seq_len]]])
                            matched = [row + val for row in matched]
                            # print(matched)
                            s = ",".join(["?"] * len(matched[0]))
                            cur = cur.executemany("insert into matched_sequences_{} values({})".format(seq_len, s), matched)
                            empty[seq_len] = False

                if early_exit:
                    break
            
            for event in range(total_events - 1):
                cur = cur.execute("delete from matched_sequences_{}".format(event))

    print("TOTAL FILTER TIME {}".format(total_filter_time))
    print("OVERHEAD", total_other_time)
    for key in length_dicts:
        print(key, len(length_dicts[key]), np.mean(length_dicts[key]))
    
    # dump total_filter_times dict as pickle
    with open("total_filter_times.pickle", "wb") as f:
        pickle.dump(total_filter_times, f)
    
    print("TOTAL FILTER EVENTS: ", sum([len(length_dicts[key]) for key in length_dicts]))
    print("TOTAL FILTERED ROWS: ", sum([np.sum(length_dicts[key]) for key in length_dicts]))

    if len(matched_events) > 0:

        matched_events = [item for sublist in matched_events for item in sublist]
        matched_events = batch[matched_events].select(["__row_count__", time_col, by]) if by is not None else batch[matched_events].select(["__row_count__", time_col])
        
        events = [matched_events[i::total_events] for i in range(total_events)]
        for i in range(total_events):
            if i != 0 and by is not None:
                events[i] = events[i].drop(by)
            events[i] = events[i].rename({"__row_count__" : event_names[i] + "___row_count__", time_col : event_names[i] + "_" + time_col})
        matched_events = polars.concat(events, how = 'horizontal')
        return matched_events
    
    else:
        return None


