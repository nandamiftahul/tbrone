Usage:
eprocess <hdf5 filename with path> <target moment> -corr -comp <moment1> -conv <moment2>
ex)
eprocess xxxxxx.h5 DBZX -corr -comp DBZH -conv DBZV
   = DBZX to be applied ZDR&RHOHV correction
     -> DBZX to be max-composited with DBZH
     -> processed DBZX to be copied to DBZV
eprocess xxxxxx.h5 DBZX -comp DBZH
   = DBZX to be max-composited with DBZH
  (ZDR&RHOHV correction not applied, DBZX not copied to any moments)

Note: 
DBTE to be specified as TX, DBZE to be specified as DBZX

Compile:
h5cc -Wall -O2 -Wextra -std=c11 eprocess.c -lm -o eprocess
