Usage:
median <hdf5 filename with path> <moment1> <moment2> ... <momentN>
ex)
median xxxxxx.h5 DBZH VRADH DBZX

Note: 
DBTE to be specified as TX, DBZE to be specified as DBZX

Compile:
h5cc -Wall -O2 median_***.c -o median -lm

