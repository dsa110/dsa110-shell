#!/bin/bash

# v2.4.0-rc8 is the good dsa-25 one

#for corr in 'corr01' 'corr02' 'corr03' 'corr04' 'corr05' 'corr06' 'corr07' 'corr08' 'corr09' 'corr10' 'corr11' 'corr12' 'corr13' 'corr14' 'corr15' 'corr16' 'corr18' 'corr19' 'corr21' 'corr22'; do
#
#    screen -S ${corr} -dm bash -c "/home/ubuntu/proj/dsa110-shell/deploy install ${corr}.pro.pvt v3.1.0-rc18; sleep 300"
#
#done

#for corr in '03' '04' '05' '06' 'h07' '08' '10' '11' '12' '14' '15' '16' '18' '19' '21' '22'; do

#for corr in '18'; do

for corr in 'h02' 'h09' 'h13'; do
	    
    screen -S ${corr} -dm bash -c "/home/ubuntu/proj/dsa110-shell/deploy install h${corr}.pro.pvt v3.1.0-rc33; sleep 300"

done

#for corr in '01' '02' '09' '13'; do

#    screen -S ${corr} -dm bash -c "/home/ubuntu/proj/dsa110-shell/deploy install corr${corr}.pro.pvt v3.1.0-rc31; sleep 300"

#done
