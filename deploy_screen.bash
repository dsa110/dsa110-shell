#!/bin/bash

# v2.4.0-rc8 is the good dsa-25 one

for corr in 'corr01' 'corr02' 'corr03' 'corr04' 'corr05' 'corr06' 'corr07' 'corr08' 'corr09' 'corr10' 'corr11' 'corr12' 'corr13' 'corr14' 'corr15' 'corr16' 'corr17' 'corr18' 'corr19' 'corr20' 'corr21' 'corr22'; do

    screen -S ${corr} -dm bash -c "/home/ubuntu/proj/dsa110-shell/deploy install ${corr}.sas.pvt v3.0.0-rc154; sleep 300"

done
