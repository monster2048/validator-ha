
for i in {1..3}
do
slot_err=`curl http://localhost:8500 -X POST -H "Content-Type: application/json" -d ' {"jsonrpc":"2.0","id":1, "method":"getHealth"} '|jq '.error'`
slot_ok=`curl http://localhost:8500 -X POST -H "Content-Type: application/json" -d ' {"jsonrpc":"2.0","id":1, "method":"getHealth"} '|jq '.result'`
slot_good="\"ok\""
if [ "$slot_err" = "null" ] && [ $slot_ok = $slot_good ]; then
        if [ -e done_fail ]; then
                failover-from-backup.sh
                echo `date` "restore to main node!" >> log
                rm done_fail
        fi
else
        if [ ! -e done_fail ]; then
                failover-to-backup.sh
                echo `date` "failover to backup node!" >> log
                touch done_fail
        fi
fi
sleep 20
done
