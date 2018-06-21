#!/usr/bin/sh
if [ ! -e $1 ]; then
        echo "File $1 is does not exist"
        exit 1
fi
export model=`echo $1 | sed 's/^.*\///' | sed 's/\..*$//'`
echo $model
echo "1. Generate the wrapper file $model.v"
pureview -batch -generate all model -genoutput $model.v $1 &> /dev/null

echo "2. Generate tb.v"
echo "module tb;" >> tb.v
echo "  model model();" >> tb.v
echo "endmodule" >> tb.v
export FLEXLM_DIAGNOSTICS=3
echo "3. Run the tb"
xrun *.v -denali $DENALI &> log
grep "Checkout succ" log
rm -rf $model.v tb.v *log xrun.* flex.* xcelium.d INCA_libs
