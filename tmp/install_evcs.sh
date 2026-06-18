  GNU nano 7.2                                                  install_evcs.sh
#!/bin/bash

rm /opt/victronenergy/dbus-systemcalc-py/delegates/dynamicess.py
wget -P /opt/victronenergy/dbus-systemcalc-py/delegates/ https://raw.githubusercontent.com/victronenergy/dbus-systemcalc-py/refs/heads/dmanner/dess_extraction/delegates/dynamicess.py

mkdir /opt/victronenergy/venus-dynamicess
wget -O /tmp/dess.zip https://github.com/victronenergy/venus-dynamicess/archive/refs/heads/main.zip
unzip -o /tmp/dess.zip -d /opt/victronenergy/venus-dynamicess
cp -r /opt/victronenergy/venus-dynamicess/venus-dynamicess-main/* /opt/victronenergy/venus-dynamicess/
rm -rf /opt/victronenergy/venus-dynamicess/venus-dynamicess-main
mkdir /opt/victronenergy/venus-dynamicess/aiovelib
cp -f /opt/victronenergy/dbus-mqtt-integrations/aiovelib/* /opt/victronenergy/venus-dynamicess/aiovelib/
rm /tmp/dess.zip

chmod a+x /opt/victronenergy/venus-dynamicess/tmp/service/run
chmod a+x /opt/victronenergy/venus-dynamicess/tmp/service/log/run
chmod a+x /opt/victronenergy/venus-dynamicess/dynamicess.py

if ! grep -Fxq 'ln -sf /opt/victronenergy/venus-dynamicess/tmp/service /service/venus-dynamicess' /data/rc.local; then
		if [ ! -f /data/rc.local ]; then
			touch /data/rc.local
			chmod a+x /data/rc.local
			echo "#!/bin/bash" > /data/rc.local
		fi
		echo "" >> /data/rc.local
        echo "#Temporary service for DESS" >> /data/rc.local
        echo "ln -sf /opt/victronenergy/venus-dynamicess/tmp/service /service/venus-dynamicess" >> /data/rc.local
		ln -sf /opt/victronenergy/venus-dynamicess/tmp/service /service/venus-dynamicess
fi

svc -t /service/dbus-systemcalc-py
svc -u /service/venus-dynamicess