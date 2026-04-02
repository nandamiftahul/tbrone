#!/bin/sh

# input pipe wrapper, which allowes prepping/pre-processing input data via some
# 3-d party tools

program_name=`basename $0`
program_version="1.0"

log_time_stamp()
{
  echo -n `date +%b' '%d' '%T` `hostname`
}

if [ -z "$IRIS_LOG" ]; then
    IRIS_LOG="/var/log/irisrda"
fi

if [ -z "$IRIS_PIPES" ]; then
    IRIS_PIPES="/usr/libexec/vaisala/pipes"
fi

# Change it when replicating!
#pipe_name=${program_name%%.sh}
pipe_name="sdthd"

log_file="$IRIS_LOG/${program_name}.log"
pipe_log_file="$IRIS_LOG/${pipe_name}.log"

# Comment next line out when contineout log is not needed
APPEND_TO_LOG="no"
if [[ "$APPEND_TO_LOG" != "yes" ]]
then
  echo "`log_time_stamp` Resetting log" | tee $log_file
else
  echo "`log_time_stamp` ==============================================================================" | tee -a $log_file
fi

if [ -z ${program_name/*.sh/sh} ]
then
   echo "This script name should have .sh suffix" | tee -a $log_file
   exit 1
fi

# XXX Change it when replicating!
special_message="2D_SD(PhiDP) filter for IRIS RAW"

# Change it when replicating!
pipe="$IRIS_PIPES/$pipe_name"


usage()
{
    echo "\
$program_name: too few arguments
Try \`$program_name --help' for more information." 1>&2
    exit 1
}

version()
{
    echo "$program_name $program_version"
    exit 0
}

help()
{
    echo "\
Convert $special_message to IRIS RAW.

Usage: $program_name -i input_file -o output_file

  -i [ --ip ] arg    full path to input file
  -o [ --op ] arg    full path to output file
  -h [--help]        display this help and exit
   v [--version]     output version information and exit
" 
  exit 0
}

# ==================================================================
# Parse arguments

while [ $# -ne 0 ]; do
    case "$1" in
        -i)
            shift
            if [ -z "$input" ]; then
                input="$1"
            else
                echo "$program_name: Input file name is already set.\
 Ignoring extra name: $1" 1>&2
            fi
            ;;
        --ip=*)
            if [ -z "$input" ]; then
                input=${1##--ip=}
            else
                echo "$program_name: Input file name is already set.\
 Ignoring extra name: ${1##--ip=}" 1>&2
            fi
            ;;
        -o)
            shift
            if [ -z "$output" ]; then
                output="$1"
            else
                echo "$program_name: Output file name is already set.\
 Ignoring extra name: $1" 1>&2
            fi
            ;;
        --op=*)
            if [ -z "$output" ]; then
                output=${1##--op=}
            else
                echo "$program_name: Output file name is already set.\
 Ignoring extra name: ${1##--op=}" 1>&2
            fi
            ;;
        -?|-h|--help)
            help
            ;;
        --version)
            version
            ;;
        *)
            echo "$program_name: Unknown option.  Ignoring extra parameter: $1" 1>&2
        ;;
    esac
    shift
done

if  [ -z "$input" ] || [ -z "$output" ]; then
    usage
fi

echo "`log_time_stamp` $special_message $program_name $program_version" | tee -a $log_file 

#check if pipe executable exist

if [[ ! -f $pipe ]]
then
    echo "Pipe $pipe does not exist." | tee -a $log_file
    exit 1
fi

#check if $input file exists
if [[ ! -f $input ]]
then
    echo "Input file $input does not exist." | tee -a $log_file
    exit 1
fi

# wait for file transfer to complete
sleep 1

# Copy incoming to local archive
#cp $input /srv/iris_data/archive/incoming

# Copy incoming to remote archive
#cp $input /srv/iris_data/to_archive

# Do the actual convertion
$pipe -i $input -o $output 2>&1 | tee -a $log_file
cat $pipe_log_file >> $log_file
rm -f $pipe_log_file 1>&2

exit 0
