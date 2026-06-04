#!/usr/bin/sh
#
# print_license.sh - list the license features available on a FlexLM server
#                    and how many of each are free.
#
# Queries the license server with `lmstat -a` and prints, for every counted
# feature, the number of licenses issued / in use / available.  A trailing
# summary reports how many distinct features were found and the total number
# of free licenses across all of them.
#
# Usage:
#   print_license.sh [-lic <port@host | license_file>] [-lmstat <path>] [-free]
#
#   -lic     license source to query (default: $LM_LICENSE_FILE, then
#            $SNPSLMD_LICENSE_FILE, then 27020@localhost)
#   -lmstat  path to the lmstat binary (default: lmstat from PATH, then the
#            bundled Synopsys SCL copy)
#   -free    only list features that currently have >=1 license available

SCRIPT=`basename $0`
src=''
lmstat=''
free_only=0

while [ "$#" -gt 0 ]; do
        case `echo $1 | tr "[A-Z]" "[a-z]"` in
                -lic)
                        src=$2
                        shift
                        ;;
                -lmstat)
                        lmstat=$2
                        shift
                        ;;
                -free)
                        free_only=1
                        ;;
                -h|-help|--help)
                        grep '^#' "$0" | sed 's/^#//'
                        exit 0
                        ;;
                *)
                        echo "$SCRIPT: Unknown option \"$1\""
                        exit 1
        esac
        shift
done

# --- resolve the license source -------------------------------------------
if [ -z "$src" ]; then
        if [ -n "$LM_LICENSE_FILE" ]; then
                src=$LM_LICENSE_FILE
        elif [ -n "$SNPSLMD_LICENSE_FILE" ]; then
                src=$SNPSLMD_LICENSE_FILE
        else
                src=27020@localhost
        fi
fi

# --- resolve the lmstat binary --------------------------------------------
if [ -z "$lmstat" ]; then
        lmstat=`command -v lmstat 2>/dev/null`
fi
if [ -z "$lmstat" ]; then
        for c in \
            "$HOME"/tools/synopsys-license/installed/scl/*/linux64/bin/lmstat; do
                if [ -x "$c" ]; then
                        lmstat=$c
                        break
                fi
        done
fi
if [ -z "$lmstat" ] || [ ! -x "$lmstat" ]; then
        echo "$SCRIPT: could not find lmstat; pass it with -lmstat <path>"
        exit 1
fi

echo "-----------------------------------------------"
echo " Available licenses"
echo " server : $src"
echo " lmstat : $lmstat"
echo "-----------------------------------------------"

# Query the server.  lmstat re-execs helpers, so strip any Windows entries
# (WSL) from PATH that would otherwise break it.
out=`PATH=/usr/bin:/bin "$lmstat" -a -c "$src" 2>&1`

# Bail out early on the common "server not reachable" error.
if echo "$out" | grep -q "Cannot connect to license server"; then
        echo "$out" | grep -i "Error\|Cannot connect"
        echo "-----------------------------------------------"
        echo " could not reach the license server"
        exit 1
fi

# Parse the feature lines:
#   Users of <FEATURE>:  (Total of N licenses issued;  Total of M licenses in use)
printf ' %-34s %8s %8s %8s\n' FEATURE ISSUED IN_USE AVAIL
echo "-----------------------------------------------"
echo "$out" | awk -v free_only="$free_only" '
        /Users of .*Total of .*licenses? issued/ {
                feat = $0
                sub(/^.*Users of +/, "", feat)
                sub(/:.*/,           "", feat)

                # ISSUED follows the FIRST "Total of" (after the "(").
                issued = $0
                sub(/^.*\(Total of +/, "", issued)
                sub(/ +license.*/,     "", issued)

                # IN_USE follows the SECOND "Total of" (after "issued;").
                used = $0
                sub(/^.*issued; +Total of +/, "", used)
                sub(/ +license.*/,            "", used)

                avail = issued - used
                if (free_only && avail <= 0)
                        next

                printf " %-34s %8d %8d %8d\n", feat, issued, used, avail
                nfeat++
                free_total += avail
        }
        END {
                printf "-----------------------------------------------\n"
                printf " %d feature(s); %d license(s) available\n", nfeat, free_total
        }
'
echo "-----------------------------------------------"
exit 0
