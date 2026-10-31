import QtQuick
import QtTest
import "../../../" as Tonearm

TestCase {
    id: test
    name: "ReadOnlyQueue"
    width: 420; height: 350; visible: true
    when: windowShown
    property var replies: []
    property int calls: 0
    function state(zone) { return {status:"ok",core:{host:"fixture"},zone:{id:zone,name:"Fixture room"},selection:{zone_id:zone,availability:"available",mode:"pinned"}} }
    function result(request, status, rows) { return {type:"queue_state",v:1,request_id:request.request_id,zone_id:request.zone_id,status:status,error:null,items:rows,revision:1,limit:100,schema:{}} }
    QtObject { id: fixture; function queue(request, callback) { test.calls++; test.replies.push({request:request, callback:callback}) } }
    Component { id: pane; Tonearm.QueuePane { width: 390; service: fixture; Rectangle { anchors.fill:parent; z:-1; color:"#181818" } } }
    function make() { return createTemporaryObject(pane,test,{roonState:state("a"),panelOpen:true}) }
    function init() { calls=0; replies=[] }
    function deliver(status,rows) { var pending=replies.shift();pending.callback(result(pending.request,status,rows)) }
    function test_hidden_and_single_flight() {
        var view=make();verify(view);compare(calls,0)
        view.expanded=true;compare(calls,1)
        view.poll();view.poll();compare(calls,1)
        view.expanded=false;view.expanded=true;compare(calls,1)
        deliver("ready",[{id:1,title:"Old view",subtitle:"Fixture"}]);compare(view.rows.length,0)
        view.poll();compare(calls,2)
        deliver("ready",[{id:2,title:"New view",subtitle:"Fixture"}]);compare(view.rows[0].id,2)
    }
    function test_timer_runs_only_while_view_is_open() {
        var view=make();wait(1100);compare(calls,0)
        view.expanded=true;compare(calls,1);deliver("ready",[])
        tryCompare(test,"calls",2,1500);deliver("ready",[])
        view.panelOpen=false;wait(1100);compare(calls,2);compare(view.rows.length,0)
    }
    function test_selection_invalidates_rows_and_old_reply() {
        var view=make();view.expanded=true
        view.roonState=state("b");compare(calls,1)
        deliver("ready",[{id:1,title:"Old room",subtitle:"Fixture"}]);compare(view.rows.length,0)
        view.poll();compare(calls,2);compare(replies[0].request.zone_id,"b")
        deliver("ready",[]);compare(view.status,"ready");verify(view.message.indexOf("No items")>=0)
        view.roonState={status:"ok",zone:{id:"fallback"},selection:{zone_id:"missing",availability:"missing"}}
        compare(view.rows.length,0);compare(view.status,"unavailable");view.poll();compare(calls,2)
    }
    function test_read_only_render_keyboard_and_close() {
        var view=make();view.expanded=true
        deliver("ready",Array.from({length:100},function(_,i){return {id:i,title:"Synthetic queue item "+i,subtitle:"Fixture artist"}}))
        compare(view.rows.length,100);verify(view.height<350)
        view.move(99);compare(view.cursor,99);view.move(1);compare(view.cursor,99)
        compare(view.activate(),true);compare(calls,1) // Enter cannot play anything.
        verify(view.back());compare(view.expanded,false);compare(view.rows.length,0)
        view.poll();compare(calls,1)
    }
    Component { id:integration; Tonearm.Integration {} }
    function test_actual_panel_keyboard_handlers() {
        var host=createTemporaryObject(integration,test)
        verify(host)
        host.queue.service=fixture;host.queue.roonState=state("a")
        host.queue.expanded=true
        deliver("ready",[{id:1,title:"One",subtitle:""},{id:2,title:"Two",subtitle:""}])
        verify(host.keys.activeFocus)
        keyClick(Qt.Key_Down);compare(host.queue.cursor,1)
        keyClick(Qt.Key_Return);keyClick(Qt.Key_Space)
        compare(host.browseActivations,0);compare(calls,1)
        keyClick(Qt.Key_Escape);compare(host.queue.expanded,false);compare(host.closed,false)
        keyClick(Qt.Key_Escape);compare(host.closed,true)
    }
    function test_scaled_layout_contains_its_body() {
        var view=make();view.unit=2;view.expanded=true
        deliver("ready",[{id:1,title:"Scaled fixture",subtitle:"Artist"}]);wait(20)
        var body=view.children[1]
        compare(view.height,body.y+body.height)
    }
    function test_offscreen_visual_states() {
        var view=make();view.expanded=true
        deliver("ready",[{id:1,title:"Synthetic queue item with a title longer than this view can display",subtitle:"Fixture artist"},{id:2,title:"Second synthetic item",subtitle:"Fixture album"}])
        wait(20)
        var image=grabImage(view);compare(image.width,390);image.save("/tmp/tonearm-queue-view-ready.png")
        view.roonState={status:"ok",zone:null,selection:{zone_id:"missing",availability:"missing"}}
        wait(20)
        var missing=grabImage(view);compare(missing.width,390);missing.save("/tmp/tonearm-queue-view-unavailable.png")
    }
    function test_malformed_error_and_disconnect() {
        var view=make();view.expanded=true
        var pending=replies.shift();pending.callback({status:"ready",items:[]})
        compare(view.status,"error");compare(view.rows.length,0)
        view.roonState={status:"unreachable",zone:null};compare(view.status,"disconnected")
        view.poll();compare(calls,1)
    }
}
