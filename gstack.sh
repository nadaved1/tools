#!/bin/bash
SCRIPT=`basename $0`
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
		*)
			echo "$SCRIPT: Unknown option $1"
			exit 1
  	esac
   	shift
done

while : 
do
	gstack $1
	retVal=$?
	if [ $retVal -ne 0 ]; then
		echo "$pid Done"
		break
	fi
	sleep 10
done
echo "$1 Finished.."