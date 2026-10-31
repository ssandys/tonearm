const test = require('node:test')
const assert = require('node:assert/strict')
const M = require('../Model.js')
const states = require('../tools/visual-preview/states.json')

test('empty body distinguishes startup, pairing, outage, loading, missing pin and idle', () => {
  const expected = {
    initial: 'Waiting for Tonearm', connecting: 'Connecting to Roon…',
    unpaired: 'Authorization required', disconnected: 'Connection unavailable',
    idle: 'No output selected', unknown: 'Loading outputs…', missing: 'Output unavailable'
  }
  for (const [name, title] of Object.entries(expected)) {
    assert.equal(M.emptyTitle(states[name]), title, name)
    assert.ok(M.emptyDetail(states[name]).length > 0, name + ' needs an explanation')
  }
})

test('fresh-list waiting has explicit header/tooltip without false missing warning', () => {
  for (const mode of ['auto', 'pinned']) {
    const st = {...states.unknown, selection: {...states.unknown.selection, mode}}
    assert.equal(M.headerStatus(st), 'Loading outputs…')
    assert.equal(M.tooltipText(st), 'Loading outputs… · Preview Core')
    assert.equal(M.barState(st, 1000, 1000).severity, 'ok')
    assert.ok(!M.emptyDetail(st).includes('unavailable'))
  }
})

test('connection fault takes precedence over remembered unknown or missing identity', () => {
  for (const availability of ['unknown', 'missing']) {
    const st = {...states.disconnected, selection: {...states.missing.selection, availability}}
    assert.equal(M.headerStatus(st), 'Roon Core unreachable')
    assert.equal(M.emptyTitle(st), 'Connection unavailable')
  }
  assert.equal(M.emptyTitle({...states.disconnected, status:'no_network'}), 'Connection unavailable')
})

test('pairing instructions remain available in full, beyond the elided header', () => {
  assert.equal(M.emptyDetail(states.unpaired), 'Enable Tonearm in Roon → Settings → Extensions.')
  assert.equal(M.headerStatus(states.missing), 'Pinned output unavailable')
  assert.match(M.emptyDetail(states.missing), /Choose an available output/)
})

test('legacy v1 idle and available zone without metadata keep Nothing playing', () => {
  const legacy = {...states.idle}
  delete legacy.selection
  assert.equal(M.emptyTitle(legacy), 'Nothing playing')
  assert.equal(M.emptyDetail(legacy), '')
  for (const name of ['playing', 'paused', 'fixed']) {
    assert.equal(M.emptyTitle(states[name]), 'Nothing playing')
    assert.equal(M.emptyDetail(states[name]), '')
    assert.equal(M.headerStatus(states[name]), 'Preview Core')
  }
})
