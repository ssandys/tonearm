import QtQuick
import QtTest
import qs.Commons
import "." as Tonearm

TestCase {
    id: test
    name: "CompactActionsAndQueueTime"
    width: 620; height: 500; visible: true
    when: windowShown
    property var calls: []
    function state(seconds) {
        return {status:"ok",zone:{id:"synthetic-zone",name:"Synthetic iFi",state:"playing",queue_time_remaining:seconds},
            selection:{zone_id:"synthetic-zone",availability:"available",mode:"pinned"}}
    }
    QtObject {
        id: fixture
        function browse(session, args, callback) { test.calls.push(args) }
    }
    Component {
        id: layout
        Rectangle {
            width:400;height:330;color:"#181818"
            property alias browse: browse
            property alias queue: queue
            Tonearm.BrowsePane { id:browse; x:12;y:12;width:parent.width-24;service:fixture;fontFamily:"sans-serif" }
            Tonearm.QueuePane { id:queue; x:12;y:220;width:parent.width-24;fontFamily:"sans-serif" }
        }
    }
    function make(width) {
        var host=createTemporaryObject(layout,test,{width:width || 400})
        host.browse.state=state(3572)
        host.browse.rows=[{title:"Synthetic track",subtitle:"Fixture album",can_play:true,can_navigate:false}]
        host.browse.cursor=0;host.browse.levelId=17
        host.queue.roonState=state(3572)
        test.forceActiveFocus();host.browse.editing=false
        return host
    }
    function init() { calls=[];Style.scale=1;Style.font.caption=13 }
    function test_actions_share_one_row_and_keep_explicit_target() {
        for (var action of ["play_now","play_next","add_to_queue"]) {
            var host=make(340);wait(30)
            var row=findChild(host,"browseActions"), button=findChild(host,action)
            verify(row);verify(button);verify(button.enabled)
            compare(button.y,0);verify(button.contentWidth <= button.width)
            verify(button.text.indexOf("iFi")<0)
            mouseClick(button)
            compare(calls[calls.length-1][3],"synthetic-zone")
            compare(calls[calls.length-1][5],action)
        }
    }
    function test_missing_output_disables_all_actions() {
        var host=make()
        host.browse.state={status:"ok",zone:null,selection:{zone_id:"missing",availability:"missing"}}
        for(var action of ["play_now","play_next","add_to_queue"])
            verify(!findChild(host,action).enabled)
        compare(calls.length,0)
    }
    function test_live_header_updates_without_queue_reads() {
        var host=make(), header=findChild(host.queue,"queueHeader")
        compare(header.text,"▸ Queue (59:32)")
        host.queue.roonState=state(3571);compare(header.text,"▸ Queue (59:31)")
        host.queue.roonState=state(4000);compare(header.text,"▸ Queue (1:06:40)")
        host.queue.expanded=true;compare(header.text,"▾ Queue (1:06:40)")
        host.queue.expanded=false
        host.queue.roonState=state(null);compare(header.text,"▸ Queue")
        host.queue.roonState={status:"unreachable",zone:null};compare(header.text,"▸ Queue")
        compare(calls.length,0)
    }
    function test_visual_normal_and_large_font() {
        var host=make();wait(30)
        grabImage(host).save("/tmp/tonearm-compact-ui-normal.png")
        host.queue.expanded=true;host.queue.status="ready"
        host.queue.rows=[{id:1,title:"Queued track",subtitle:"Fixture album"}]
        host.height=390;wait(30)
        grabImage(host).save("/tmp/tonearm-compact-ui-expanded.png")
        host.queue.expanded=false;host.height=330
        Style.scale=1.5;Style.font.caption=20;host.width=540;host.queue.fontSize=20;host.queue.unit=1.5;wait(30)
        for(var action of ["play_now","play_next","add_to_queue"]) {
            var button=findChild(host,action)
            verify(button.contentWidth <= button.width);compare(button.y,0)
        }
        grabImage(host).save("/tmp/tonearm-compact-ui-large.png")
    }
}
