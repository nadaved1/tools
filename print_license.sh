#!/usr/bin/sh                                                                                                 
clean=1                                                                                                       
cfg=0                                                                                                         
SCRIPT=`basename $0`                                                                                          
file=0                                                                                                        
while [ "$#" -gt 0 ]; do                                                                                      
        case `echo $1 | tr "[A-Z]" "[a-z]"` in                                                                
                -noclean)                                                                                     
                        clean=0                                                                               
                        ;;                                                                                    
                -cfg)                                                                                         
                        cfg=1                                                                                 
                        ;;                                                                                    
                -f|-file)                                                                                     
                        file=$2                                                                               
                        shift                                                                                 
                        ;;                                                                                    
                *)                                                                                            
                        echo "$SCRIPT: Unknown option \"$1\""                                                 
                        exit 1                                                                                
        esac                                                                                                  
        shift                                                                                                 
done                                                                                                          
if [ $file -eq "0" ]; then                                                                                    
        echo "Must specify a soma\\spc file with -file"                                                       
        exit 1                                                                                                
fi                                                                                                            
echo $file      | grep '\.sv$'                                                                                
retVal=$?                                                                                                     
if [ $retVal -eq "0" ]; then                                                                                  
        cfg=1                                                                                                 
fi                                                                                                            
export model=`echo $file | sed 's/^.*\///' | sed 's/\..*$//'`                                                 
echo " Model: $model"                                                                                         
\rm -rf print_lic_tmp_dir                                                                                     
mkdir print_lic_tmp_dir                                                                                       
cd print_lic_tmp_dir                                                                                          
if [ $cfg -eq "1" ]; then                                                                                     
        echo "0. Convert to .spc file"                                                                        
        pureview -batch -convert v0001 $file > $model.spc 2> /dev/null                                        
        echo "1. Generate the wrapper file $model.v"                                                          
        pureview -batch -generate all model -genoutput $model.v $model.spc &> /dev/null                       
else
        echo "1. Generate the wrapper file $model.v"
        pureview -batch -generate all model -genoutput $model.v $file &> /dev/null
fi
echo "2. Generate tb.v"
echo "module tb;" >> tb.v
echo "  model model();" >> tb.v
echo "endmodule" >> tb.v
export FLEXLM_DIAGNOSTICS=3

echo "3. Prepare CDS_LICFLTR.csh"
echo "#!/bin/csh -f" >> CDS_LICFLTR.csh
echo "switch (\$1)" >> CDS_LICFLTR.csh
echo "    case Xcelium_Single_Core:" >> CDS_LICFLTR.csh
echo "        exit 0" >> CDS_LICFLTR.csh
echo "    breaksw" >> CDS_LICFLTR.csh
echo "    default:" >> CDS_LICFLTR.csh
echo "        echo \"TRY LIC: \$*\"" >> CDS_LICFLTR.csh
echo "        exit 1" >> CDS_LICFLTR.csh
echo "    breaksw" >> CDS_LICFLTR.csh
echo "endsw" >> CDS_LICFLTR.csh
chmod u+x CDS_LICFLTR.csh
export CDS_LICFLTR=./CDS_LICFLTR.csh

echo "4. Run the tb"
file $DENALI/verilog/libdenpli.so | grep -c "64-bit" > /dev/null
if [ $? -eq "0" ]; then
        opt=-64bit
fi
xrun $opt -clean *.v -denali $DENALI &> log
grep "TRY LIC" log
cd - > /dev/null
if [ $clean -eq "1" ]; then
        \rm -rf print_lic_tmp_dir
fi
