import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const plugin = new URL('../', import.meta.url)

class Element {
  constructor(id = '') {
    this.id = id
    this.dataset = {}
    this.style = {}
    this.attributes = new Map()
    this.events = new Map()
    this.srcWrites = []
    this.value = id === 'timeline-zoom' ? '96' : '0'
    this.hidden = false
    this.clientWidth = 800
    this.scrollLeft = 0
    this.readyState = 1
    this.currentTime = 0
    this.textContent = ''
  }
  set src(value) { this.srcWrites.push(value); this.setAttribute('src', value) }
  get src() { return this.getAttribute('src') }
  setAttribute(key, value) { this.attributes.set(key, String(value)) }
  getAttribute(key) { return this.attributes.get(key) ?? null }
  removeAttribute(key) { this.attributes.delete(key) }
  querySelectorAll() { return [] }
  addEventListener(type, callback) { this.events.set(type, callback) }
  dispatch(type) { return this.events.get(type)?.({ target: this }) }
  pause() {}
  load() {}
  play() { return Promise.resolve() }
}

function viewerHarness({ fetch } = {}) {
  const elements = new Map()
  const frames = new Map()
  let nextFrame = 0
  const context = vm.createContext({
    window: { location: { pathname: '/canonical-editor/canonical_test' }, addEventListener() {} },
    document: { getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element(id))
      return elements.get(id)
    } },
    performance: { now: () => 0 },
    requestAnimationFrame(callback) { frames.set(++nextFrame, callback); return nextFrame },
    cancelAnimationFrame(id) { frames.delete(id) },
    URLSearchParams,
    fetch,
  })
  const html = fs.readFileSync(new URL('ui/canonical-editor.html', plugin), 'utf8')
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  // Execute the real page functions and event listeners without booting a server.
  vm.runInContext(script.replace(/\n      boot\(\)\n/, `
      globalThis.viewer = {
        setState(value) { state = value },
        sync: syncActiveViewer,
        seek: setPlayhead,
        paused: () => !playbackRequested,
      }
  `), context)
  return { viewer: context.viewer, elements, frames }
}

function currentState(kind = 'image') {
  const clip = {
    clip_id: 'clip_one', ordinal: 0, media_kind: kind,
    start_ms: 0, end_ms: 1000, source_in_ms: 200, source_out_ms: 1200,
  }
  return {
    projection: {
      timeline_head: { timeline_id: 'timeline_one', revision: 1 },
      timeline: { output: { duration_ms: 1000 }, tracks: [{ clips: [clip] }] },
    },
    pending: null, historical: null,
    capabilities: { media_playback: true, media_thumbnail: true },
    playback: {
      schema_version: '2', output_muted: true, audio_playback_enabled: false,
      transport_may_include_audio: true, audio_stream_attested: false,
      final_fidelity: false, available: true,
      clips: [{ clip_id: 'clip_one', status: 'playable', reason_code: null }],
    },
  }
}

test('same clip id reloads its current-head thumbnail after Save or Refresh changes revision', () => {
  const { viewer, elements } = viewerHarness()
  const state = currentState()
  viewer.setState(state)
  viewer.sync()
  const image = elements.get('source-image')
  assert.equal(image.srcWrites.length, 1)
  viewer.sync()
  assert.equal(image.srcWrites.length, 1, 'ordinary seek/render does not refetch')
  state.projection.timeline_head.revision = 2
  viewer.sync()
  assert.equal(image.srcWrites.length, 2, 'new canonical head refetches unchanged clip id')
  assert.match(image.srcWrites[0], /\/previews\/clip_one\/1$/)
  assert.match(image.srcWrites[1], /\/previews\/clip_one\/2$/, 'new head has a distinct browser cache identity')
  assert.equal(new URL(image.src, 'http://127.0.0.1:1234').search, '')
})

test('image Play, Pause, and end-of-timeline keep visible and accessible labels synchronized', () => {
  const { viewer, elements, frames } = viewerHarness()
  viewer.setState(currentState())
  viewer.sync()
  const button = elements.get('play-pause')
  button.dispatch('click')
  assert.equal(button.textContent, 'Pause')
  assert.match(button.getAttribute('aria-label'), /^Pause /)
  assert.equal(viewer.paused(), false)
  button.dispatch('click')
  assert.equal(button.textContent, 'Play')
  assert.equal(frames.size, 0)
  button.dispatch('click')
  const callback = [...frames.values()].at(-1)
  frames.clear()
  callback(1200)
  assert.equal(viewer.paused(), true)
  assert.equal(button.textContent, 'Play')
  assert.match(elements.get('source-status').textContent, /end of the displayed Timeline/)
})

test('image loading failure stops the local clock and clears the unavailable image', () => {
  const { viewer, elements, frames } = viewerHarness()
  viewer.setState(currentState())
  viewer.sync()
  elements.get('play-pause').dispatch('click')
  elements.get('source-image').dispatch('error')
  assert.equal(viewer.paused(), true)
  assert.equal(frames.size, 0)
  assert.equal(elements.get('play-pause').textContent, 'Play')
  assert.equal(elements.get('play-pause').disabled, true)
  assert.equal(elements.get('source-image').getAttribute('src'), null)
  assert.equal(elements.get('source-fallback').hidden, false)
  assert.match(elements.get('source-status').textContent, /thumbnail could not be loaded/)
})

test('a rejected video request rereads the closed reason instead of claiming a codec error', async () => {
  const denied = currentState('video')
  denied.playback.available = false
  denied.playback.reason_code = 'agent_preview_scope_denied'
  denied.playback.clips = []
  const requests = []
  const { viewer, elements } = viewerHarness({ fetch: async (url, options) => {
    requests.push({ url, options })
    return { ok: true, json: async () => denied }
  } })
  viewer.setState(currentState('video'))
  viewer.sync({ continuePlayback: true })
  await elements.get('source-video').dispatch('error')
  assert.equal(viewer.paused(), true)
  assert.equal(elements.get('play-pause').textContent, 'Play')
  assert.equal(elements.get('play-pause').disabled, true)
  assert.match(elements.get('source-status').textContent, /permission.*no longer active/)
  assert.equal(requests[0].url, '/api/sessions/canonical_test')
  assert.equal(requests[0].options.cache, 'no-store')
  assert.equal(requests[0].options.credentials, 'same-origin')
})

test('a late play rejection from the previous clip cannot pause the next clip', async () => {
  const { viewer, elements } = viewerHarness()
  const state = currentState('video')
  state.projection.timeline.output.duration_ms = 2000
  state.projection.timeline.tracks[0].clips.push({
    ...state.projection.timeline.tracks[0].clips[0],
    clip_id: 'clip_two', ordinal: 1, start_ms: 1000, end_ms: 2000,
  })
  state.playback.clips.push({ clip_id: 'clip_two', status: 'playable' })
  let rejectPrevious
  const video = elements.get('source-video')
  video.play = () => {
    video.play = () => Promise.resolve()
    return new Promise((_resolve, reject) => { rejectPrevious = reject })
  }
  viewer.setState(state)
  viewer.sync({ continuePlayback: true })
  viewer.seek(1000)
  rejectPrevious(new Error('AbortError: old source load cancelled'))
  await Promise.resolve()
  assert.equal(video.dataset.clipId, 'clip_two')
  assert.equal(viewer.paused(), false)
  assert.equal(elements.get('play-pause').textContent, 'Pause')
  assert.doesNotMatch(elements.get('source-status').textContent, /could not start/)
})

test('Pause and Play on the same clip invalidate an earlier pending play failure', async () => {
  const { viewer, elements } = viewerHarness()
  let rejectPrevious
  const video = elements.get('source-video')
  video.play = () => {
    video.play = () => Promise.resolve()
    return new Promise((_resolve, reject) => { rejectPrevious = reject })
  }
  viewer.setState(currentState('video'))
  viewer.sync({ continuePlayback: true })
  elements.get('play-pause').dispatch('click')
  elements.get('play-pause').dispatch('click')
  rejectPrevious(new Error('AbortError: old Play request cancelled'))
  await Promise.resolve()
  assert.equal(viewer.paused(), false)
  assert.equal(elements.get('play-pause').textContent, 'Pause')
})

test('a hidden previous thumbnail failure cannot stop the active video', () => {
  const { viewer, elements } = viewerHarness()
  const state = currentState('image')
  state.projection.timeline.output.duration_ms = 2000
  state.projection.timeline.tracks[0].clips.push({
    ...state.projection.timeline.tracks[0].clips[0],
    clip_id: 'clip_video', media_kind: 'video', ordinal: 1, start_ms: 1000, end_ms: 2000,
  })
  state.playback.clips.push({ clip_id: 'clip_video', status: 'playable' })
  viewer.setState(state)
  viewer.sync()
  viewer.seek(1000)
  viewer.sync({ continuePlayback: true })
  elements.get('source-image').dispatch('error')
  assert.equal(elements.get('source-image').getAttribute('src'), null)
  assert.equal(elements.get('source-video').hidden, false)
  assert.equal(viewer.paused(), false)
  assert.equal(elements.get('play-pause').disabled, false)
})

for (const kind of ['image', 'video']) {
  for (const mode of ['replace', 'history', 'restore', 'structural']) {
    test(`${kind} ${mode} view never presents current-head bytes as pending or historical source`, () => {
      const { viewer, elements } = viewerHarness()
      const state = currentState(kind)
      viewer.setState(state)
      viewer.sync()
      if (mode === 'replace') state.pending = {
        object: 'memolens.canonical_editor_pending_edit', edit: { op: 'replace_clip' },
      }
      if (mode === 'history') state.historical = { selected_revision: { revision: 1 } }
      if (mode === 'restore') state.pending = { object: 'memolens.canonical_editor_pending_restore' }
      if (mode === 'structural') state.pending = { object: 'memolens.canonical_editor_pending_structural_edit' }
      viewer.sync()
      assert.equal(elements.get('source-image').hidden, true)
      assert.equal(elements.get('source-image').getAttribute('src'), null)
      assert.equal(elements.get('source-video').hidden, true)
      assert.equal(elements.get('play-pause').disabled, true)
      assert.equal(elements.get('source-fallback').hidden, false)
      assert.equal(viewer.paused(), true)
      assert.match(elements.get('source-status').textContent,
        mode === 'history' ? /Refresh current N/ : /Save or Discard/)
    })
  }
}

for (const [code, nextAction] of [
  ['agent_preview_scope_denied', /MemoLens Desktop.*timeline\.preview_media/],
  ['agent_preview_lease_expired', /expired.*Refresh current N/],
  ['agent_preview_head_changed', /changed.*Refresh current N/],
  ['agent_preview_source_changed', /source.*changed.*MemoLens Desktop/],
  ['agent_preview_rate_limited', /limit.*wait/i],
  ['agent_preview_media_unsupported', /MP4\/H\.264/],
  ['service_unavailable', /MemoLens Desktop.*running/],
]) {
  test(`video failure ${code} gives a reason-specific next action`, () => {
    const { viewer, elements } = viewerHarness()
    const state = currentState('video')
    state.playback.available = false
    state.playback.reason_code = code
    state.playback.clips = []
    viewer.setState(state)
    viewer.sync()
    assert.match(elements.get('source-status').textContent, nextAction)
    assert.equal(elements.get('play-pause').disabled, true)
  })
}

function toolRows() {
  let module
  const rows = new Map()
  const context = vm.createContext({
    URL,
    window: { __ModuleLoader__: { load(value) { module = value } } },
    open() { throw new Error('No failure card may open a URL') },
  })
  vm.runInContext(fs.readFileSync(new URL('client.js', plugin), 'utf8'), context)
  const client = module.factory(() => ({
    createElement(type, props, ...children) { return { type, props, children } },
  }))
  client.apply({ slots: {
    inject(_name, callback) { callback() },
    register(options, component) { rows.set(options.key, component) },
  } })
  return rows
}

test('DeepSeek error cards use closed codes and do not echo messages, paths, or unsafe URLs', () => {
  const row = toolRows().get('mcp__memolens__memolens_canonical_editor_handoff')
  for (const [code, nextAction] of [
    ['canonical_editor_not_editable', /Coverage.*Timeline/],
    ['canonical_editor_not_current', /current.*Coverage.*Timeline/],
    ['agent_pairing_required', /MemoLens Desktop.*pair/i],
    ['agent_pairing_scope_denied', /MemoLens Desktop.*permission/],
    ['service_unavailable', /MemoLens Desktop.*running/],
    ['canonical_editor_project_required', /project/],
  ]) {
    const result = row({ block: { kind: 'result', isError: true, content: [{
      type: 'text', text: JSON.stringify({ error: {
        code, message: 'SECRET /Users/private/file http://attacker.invalid',
      } }),
    }] } })
    const text = JSON.stringify(result)
    assert.match(text, nextAction)
    assert.doesNotMatch(text, /SECRET|\/Users\/|attacker\.invalid/)
    assert.equal(result.props['data-state'], 'error')
    assert.equal(result.children.some(child => child.props?.key === 'open'), false)
  }
  for (const block of [
    { kind: 'result', isError: true, content: [{ type: 'text', text: 'SECRET /Users/private' }] },
    { kind: 'result', content: [{ type: 'text', text: JSON.stringify({
      error: { code: '__proto__', message: 'SECRET' },
      uiHandoff: { url: 'https://attacker.invalid/SECRET' },
    }) }] },
  ]) {
    const result = row({ block })
    assert.match(JSON.stringify(result), /Inspect.*retry/i)
    assert.doesNotMatch(JSON.stringify(result), /SECRET|attacker/)
    assert.equal(result.children.some(child => child.props?.key === 'open'), false)
  }
})
