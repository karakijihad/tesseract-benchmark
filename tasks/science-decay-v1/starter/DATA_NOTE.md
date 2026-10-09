# About data.csv

A Geiger counter was started at time zero next to a sample that contains one radioactive species. The counter has been counting ever since, and `data.csv` holds one row per counting bin.

- `bin_start_s` and `bin_end_s` give the bin edges in seconds since the start. Every bin is the same width.
- `counts` is the number of detector clicks recorded in that bin.

The count rate in a bin is the decaying signal from the sample plus a steady background from the surroundings. The background rate is the same in every bin but its size is not known. Counting is random, so each bin carries Poisson noise. Detector dead time is small enough to ignore.
