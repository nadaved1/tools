#!/bin/bash
SCRIPT=`basename $0`
step=10
log=gstack.log
while [ "$#" -gt 0 ]; do
	case `echo $1 | tr "[A-Z]" "[a-z]"` in
      	-pid|-p)
			pid=$2
		  	shift
		  	;;
		-sleep)
			step=$2
			shift
			;;
		-log)
			log=$2
			shift
			;;
		-help|-h)
			echo "$SCRIPT -pid <pid_number> [-sleep <sec>]"
			exit 0
			;;
		*)
			echo "$SCRIPT: Unknown option $1"
			exit 1
  	esac
   	shift
done

echo " Options used:" | tee $log
echo "    step=$step" | tee -a $log
echo "    pid=$pid"   | tee -a $log
echo "------------- gstack -------------" | tee -a $log

while : 
do
	ps -p $pid >& /dev/null
	retVal=$?
	if [ $retVal -ne 0 ]; then
		echo "$pid Done" | tee -a $log
		break
	fi
	gstack $pid | tee -a $log
	sleep $step
done
echo "$1 Finished.." | tee -a $log
