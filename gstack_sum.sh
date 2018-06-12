#!/usr/bin/sh

# Get a list of all the functions
input=$1
threshold=1
grep "^#" $input | sed 's/.* in //' | sed 's/\s*(.*//' | sort -u > functions_list.txt

# Count all appearances
while read line; do
	num=$(grep -c $line $input)
	if [ $num -gt $threshold ]; then
		printf ' %-40s %d\n' $line $num >> usage_list.txt
	fi
done < functions_list.txt

# Sort by count
echo "-----------------------------------------------"
echo " List functions by their count"
echo " input file: $input"
echo " Threshold: $threshold"
echo "-----------------------------------------------"
sort -rnk 2 usage_list.txt
echo "-----------------------------------------------"

rm -rf functions_list.txt usage_list.txt
exit 0
