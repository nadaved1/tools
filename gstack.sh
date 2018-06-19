#!/bin/bash
SCRIPT=`basename $0`
step=10
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

while : 
do
	echo "------------- gstack -------------"
	gstack $pid
	retVal=$?
	if [ $retVal -ne 0 ]; then
		echo "$pid Done"
		break
	fi
	sleep $step
done
echo "$1 Finished.."
