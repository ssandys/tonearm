// Synthetic display contracts; no Core schema or countdown acceptance.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const Q = require('../QueueModel.js')
function state(time = null) {
  return {status: 'ok', selection: {availability: 'available', zone_id: 'z'},
    zone: {id: 'z', queue_time_remaining: time},
    queue: {status: 'ready', zone_id: 'z', items: []}}
}
test('optional selected-zone scalar, zero real, malformed hidden', () => {
  for (const x of [null, undefined, true, NaN, Infinity, -1, 2147483649, '12'])
    assert.equal(Q.header(state(x)), 'Queue')
  assert.equal(Q.header(state(0)), 'Queue (0:00)')
  assert.equal(Q.header(state(3723.5)), 'Queue (62:03)')
  for (const availability of ['unknown', 'missing', 'none']) {
    const s = state(42); s.selection.availability = availability
    assert.equal(Q.header(s), 'Queue')
    assert.deepEqual(Q.rows(s), [])
  }
  const s = state(42); s.status = 'unreachable'
  assert.equal(Q.header(s), 'Queue')
})
test('distinct pushed states, older daemon and context mismatch fail closed', () => {
  assert.equal(Q.status({status: 'ok'}), 'unavailable')
  for (const word of ['loading', 'ready', 'error', 'unknown', 'unavailable']) {
    const s = state(); s.queue.status = word
    assert.equal(Q.status(s), word)
    assert.equal(typeof Q.message(s), 'string')
  }
  assert.equal(Q.message(state()), 'Queue is empty')
  const s = state(); s.queue.zone_id = 'old'; s.queue.items = [{title: 'old'}]
  assert.deepEqual(Q.rows(s), [])
})
test('bounded rows and single read-only cursor', () => {
  const s = state(); s.queue.items = Array(105).fill({title: 'synthetic'})
  assert.equal(Q.rows(s).length, 100)
  assert.equal(Q.cursor(0, -1, 2), 0)
  assert.equal(Q.cursor(0, 10, 2), 1)
  assert.equal(Q.cursor(0, 1, 0), -1)
})
test('pane has no backend or action path; Panel composes with Browse signals', () => {
  const qml = fs.readFileSync('QueuePane.qml', 'utf8')
  assert.doesNotMatch(qml, /Timer\s*\{|tonearmctl|Process\s*\{|ComponentBehavior|Service\./)
  assert.equal((qml.match(/\bText\s*\{/g) || []).length,
    (qml.match(/textFormat: Text.PlainText/g) || []).length)
  const panel = fs.readFileSync('Panel.qml', 'utf8')
  assert.match(panel, /onActivateRequested: if \(!queuePane.expanded\)/)
  for (const signal of ['HasContent', 'Editing', 'Busy'])
    assert.match(panel, new RegExp('on' + signal + 'Changed'))
})
test('restoring BrowsePane visibility resumes pending bounded art retries', () => {
  const browse = fs.readFileSync('BrowsePane.qml', 'utf8')
  const panel = fs.readFileSync('Panel.qml', 'utf8')
  assert.match(panel, /visible: browsePane\.hasContent && !queuePane\.expanded/)
  assert.match(browse, /onVisibleChanged: if \(visible\) root\._scheduleArt\(\)/)
  assert.match(browse, /function _scheduleArt\(\)/)
  assert.match(browse, /function _refreshArt\(\)/)
})
