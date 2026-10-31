import QtQuick
import QtTest
import "./fixture" as Fixture
import "./view" as View
TestCase {
    id:test
    name:"QueuePreviewDiagnostics"
    width:420;height:350;visible:true
    when:windowShown
    Component { id:host; View.Integration {} }
    function init() {
        Fixture.Service.history={sequence:0,events:[]}
        Fixture.Service.queueMode="ready";Fixture.Service.queueHold=false;Fixture.Service.queuePending=[]
    }
    function test_key_history_survives_popup_close() {
        var view=createTemporaryObject(host,test);verify(view)
        view.queue.service=Fixture.Service
        view.queue.roonState={status:"ok",zone:{id:"synthetic"}}
        view.queue.expanded=true
        compare(view.queue.rows.length,2);verify(view.keys.activeFocus)
        keyClick(Qt.Key_Down);keyClick(Qt.Key_Return);keyClick(Qt.Key_Space)
        var before=Fixture.Service.history.events
        compare(before.filter(function(e){return e.kind==="key-activate"}).length,2)
        compare(view.browseActivations,0)
        keyClick(Qt.Key_Escape);keyClick(Qt.Key_Escape)
        compare(view.opened,false);compare(view.queue.cursor,-1)
        var events=Fixture.Service.history.events
        verify(events.some(function(e){return e.kind==="key-move" && e.focus && e.queueOpen}))
        verify(events.some(function(e){return e.kind==="popup-open" && !e.open}))
        compare(events.filter(function(e){return e.kind==="key-activate"}).length,2)
    }
    function test_blocked_verb_and_instance_are_retained() {
        var id=Fixture.Service.instanceId
        Fixture.Service.send("playpause");Fixture.Service.note("popup-open",{open:false})
        compare(Fixture.Service.instanceId,id)
        compare(Fixture.Service.history.events[0].kind,"blocked-send")
        compare(Fixture.Service.history.events[0].verb,"playpause")
        compare(Fixture.Service.history.events[1].open,false)
    }
    function test_held_reply_keeps_request_identity() {
        Fixture.Service.queueHold=true
        var reply=null
        Fixture.Service.queue({request_id:"synthetic-1",zone_id:"synthetic-zone"},function(value){reply=value})
        compare(Fixture.Service.queuePending.length,1);compare(reply,null)
        Fixture.Service.flushQueue();compare(reply.request_id,"synthetic-1")
        var events=Fixture.Service.history.events
        compare(events[0].kind,"queue-request");compare(events[0].zone,"synthetic-zone")
        compare(events[1].kind,"queue-flush");compare(events[1].request,"synthetic-1")
    }
}
