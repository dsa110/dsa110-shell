#!/bin/bash
#

for corr in 'corr00' 'corr01' 'corr02' 'corr03' 'corr04' 'corr05' 'corr06' 'corr07' 'corr08' 'corr09' 'corr10' 'corr11' 'corr12' 'corr13' 'corr14' 'corr15' 'corr16' 'corr17' 'corr18' 'corr19' 'corr20' 'corr21' 'corr22' 'corr23'; do
    echo "Status of $corr:"
    ssh -q -o "StrictHostKeyChecking no" $corr.sas.pvt '( pip list | grep dsa110-T2 ; pip list | grep dsa110-antpos ; pip list | grep pyuvdata ; pip list | grep dsa110-pyutils ; pip list | grep dsa110-calib ; pip list | grep dsa110-meridian-fs )'
    echo " "
done

    
