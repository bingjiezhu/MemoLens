import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const plugin = new URL('../', import.meta.url)

class Element {
  constructor(id = '', tagName = 'div', ownerDocument = null) {
    this.id = id
    this.ownerDocument = ownerDocument
    this.tagName = tagName.toUpperCase()
    this.className = ''
    this.children = []
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' ') },
      contains: name => this.className.split(/\s+/).includes(name),
    }
    this.dataset = {}
    this.style = {}
    this.attributes = new Map()
    this.events = new Map()
    this.srcWrites = []
    this.value = id === 'timeline-zoom' ? '96' : '0'
    this.hidden = false
    this.open = false
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
  append(...nodes) {
    for (const node of nodes) {
      node.parentElement = this
      this.children.push(node)
    }
  }
  prepend(...nodes) {
    for (const node of nodes) node.parentElement = this
    this.children.unshift(...nodes)
  }
  replaceChildren(...nodes) {
    if (this.children.some(node => node.contains(this.ownerDocument?.activeElement))) {
      this.ownerDocument.activeElement = this.ownerDocument.body
    }
    for (const node of this.children) node.parentElement = null
    this.children = []
    this.append(...nodes)
  }
  remove() {
    if (this.parentElement) {
      if (this.contains(this.ownerDocument?.activeElement)) {
        this.ownerDocument.activeElement = this.ownerDocument.body
      }
      this.parentElement.children = this.parentElement.children.filter(node => node !== this)
      this.parentElement = null
    }
  }
  contains(node) { return node === this || this.children.some(child => child.contains(node)) }
  focus() {
    if (this.ownerDocument && !this.disabled) this.ownerDocument.activeElement = this
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(value => value.trim())
    const matches = node => selectors.some(value => value.startsWith('.')
      ? node.classList.contains(value.slice(1))
      : node.tagName.toLowerCase() === value.toLowerCase())
    const descendants = this.children.flatMap(node => [node, ...node.querySelectorAll('*')])
    return selector === '*' ? descendants : descendants.filter(matches)
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null }
  getBoundingClientRect() { return { left: 0 } }
  addEventListener(type, callback) { this.events.set(type, callback) }
  dispatch(type, details = {}) {
    return this.events.get(type)?.({ target: this, preventDefault() {}, stopPropagation() {}, ...details })
  }
  pause() {}
  load() {}
  play() { return Promise.resolve() }
}

function viewerHarness({ fetch } = {}) {
  const html = fs.readFileSync(new URL('ui/canonical-editor.html', plugin), 'utf8')
  const staticElements = new Map([...html.matchAll(/<([\w-]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)]
    .map(match => [match[2], { tagName: match[1], markup: match[0] }]))
  const elements = new Map()
  const frames = new Map()
  const windowEvents = new Map()
  let nextFrame = 0
  const document = {
    hidden: false,
    getElementById(id) {
      if (!staticElements.has(id)) return null
      if (!elements.has(id)) {
        const { tagName, markup } = staticElements.get(id)
        const node = new Element(id, tagName, document)
        for (const attribute of ['min', 'max', 'value', 'tabindex', 'type']) {
          const match = markup.match(new RegExp(`\\b${attribute}="([^"]+)"`))
          if (match) {
            node[attribute] = match[1]
            node.setAttribute(attribute, match[1])
          }
        }
        elements.set(id, node)
        const parent = ['save', 'discard'].includes(id)
          ? document.getElementById('pending') : document.body
        parent.append(node)
      }
      return elements.get(id)
    },
    createElement: tagName => new Element('', tagName, document),
  }
  document.body = new Element('', 'body', document)
  document.activeElement = document.body
  const context = vm.createContext({
    window: {
      location: { pathname: '/canonical-editor/canonical_test' },
      addEventListener(type, callback) { windowEvents.set(type, callback) },
    },
    document,
    performance: { now: () => 0 },
    requestAnimationFrame(callback) { frames.set(++nextFrame, callback); return nextFrame },
    cancelAnimationFrame(id) { frames.delete(id) },
    URLSearchParams,
    fetch,
  })
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  // Execute the real page functions and event listeners without booting a server.
  vm.runInContext(script.replace(/\n      boot\(\)\n/, `
      globalThis.viewer = {
        setState(value) { state = value },
        setBusy,
        render,
        sync: syncActiveViewer,
        seek: setPlayhead,
        paused: () => !playbackRequested,
      }
  `), context)
  return { viewer: context.viewer, elements, frames, document, windowEvents }
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

function twoClipState(kind = 'image') {
  const state = currentState(kind)
  state.projection.project_id = 'project_one'
  state.projection.eligible_for_current_use = true
  state.projection.replacement_candidates = {}
  state.projection.timeline.output.duration_ms = 2000
  state.projection.timeline.tracks[0].clips.push({
    ...state.projection.timeline.tracks[0].clips[0],
    clip_id: 'clip_two', ordinal: 1, start_ms: 1000, end_ms: 2000,
  })
  state.playback.clips.push({ clip_id: 'clip_two', status: 'playable', reason_code: null })
  Object.assign(state.capabilities, {
    move_clip: true, split_clip: true, delete_clip: true,
    save: true, inspect_history: true, restore_revision: true,
  })
  return state
}

function visibleInspectorIds(elements) {
  return elements.get('clips').querySelectorAll('.clip')
    .filter(card => !card.hidden).map(card => card.dataset.clipId)
}

function splitPreviewState(state) {
  const clips = state.projection.timeline.tracks[0].clips
  return {
    ...state,
    pending: {
      object: 'memolens.canonical_editor_pending_structural_edit',
      structural_edit: { op: 'split_clip', clip_id: clips[0].clip_id, source_split_ms: 700 },
      preview: {
        object: 'canonical_timeline.pending_preview',
        timeline: { output: { duration_ms: 2000 }, tracks: [{ clips: [
          { ...clips[0], clip_id: 'pending_split_left', end_ms: 500, source_out_ms: 700 },
          { ...clips[0], clip_id: 'pending_split_right', ordinal: 1, start_ms: 500, source_in_ms: 700 },
          { ...clips[1], ordinal: 2 },
        ] }] },
      },
    },
  }
}

test('selecting a timeline clip shows its inspector while retaining all clip forms', () => {
  const { viewer, elements } = viewerHarness()
  viewer.setState(twoClipState())
  viewer.render()
  const cards = [...elements.get('clips').children]
  assert.deepEqual(cards.map(card => card.dataset.clipId), ['clip_one', 'clip_two'])
  assert.deepEqual(visibleInspectorIds(elements), ['clip_one'])
  assert.match(elements.get('selected-clip-label').textContent, /^Clip 1 of 2(?: ·|$)/)

  const timelineClips = elements.get('timeline-track').querySelectorAll('.timeline-clip')
  assert.deepEqual(timelineClips.map(clip => clip.querySelector('.timeline-clip-copy').textContent), ['Clip 1', 'Clip 2'])
  assert.equal(timelineClips[0].querySelector('.timeline-clip-body').getAttribute('aria-label'), 'image clip 1, 1000 milliseconds')
  timelineClips[1].querySelector('.timeline-clip-body').dispatch('click')
  assert.deepEqual(visibleInspectorIds(elements), ['clip_two'])
  assert.match(elements.get('selected-clip-label').textContent, /^Clip 2 of 2(?: ·|$)/)
  assert.equal(timelineClips[0].querySelector('.timeline-clip-body').getAttribute('aria-pressed'), 'false')
  assert.equal(timelineClips[1].querySelector('.timeline-clip-body').getAttribute('aria-pressed'), 'true')
  assert.deepEqual(elements.get('clips').children, cards, 'selection retains the existing exact-value forms')
})

test('primary clip actions precede advanced settings in reading and keyboard order', () => {
  const { viewer, elements } = viewerHarness()
  viewer.setState(twoClipState('video'))
  viewer.render()
  const controls = elements.get('clips').children[0].querySelector('.controls')
  assert.ok(controls.children[0].classList.contains('structure-controls'))
  assert.ok(controls.children[1].classList.contains('precise-controls'))
})

test('resizing preserves open clip settings, unstaged values, focus and timeline controls', () => {
  const { viewer, elements, document, windowEvents } = viewerHarness()
  windowEvents.get('resize')() // Safe before the initial state has loaded.
  viewer.setState(twoClipState('video'))
  viewer.render()
  const card = elements.get('clips').children[0]
  const precise = card.querySelector('.precise-controls')
  const input = precise.querySelector('input')
  const timelineClip = elements.get('timeline-track').querySelector('.timeline-clip-body')
  precise.open = true
  input.value = '350'
  input.focus()
  elements.get('visual-timeline').clientWidth = 1200
  windowEvents.get('resize')()
  assert.equal(elements.get('timeline-canvas').style.width, '1200px')
  assert.equal(elements.get('clips').children[0], card)
  assert.equal(precise.open, true)
  assert.equal(input.value, '350')
  assert.equal(document.activeElement, input)
  timelineClip.focus()
  elements.get('visual-timeline').clientWidth = 390
  windowEvents.get('resize')()
  assert.equal(elements.get('timeline-canvas').style.width, '720px')
  assert.equal(elements.get('timeline-track').querySelector('.timeline-clip-body'), timelineClip)
  assert.equal(document.activeElement, timelineClip)
  assert.equal(precise.open, true)
  assert.equal(input.value, '350')
})

test('seeking across a clip boundary synchronizes the selected inspector through the timeline end', () => {
  const { viewer, elements } = viewerHarness()
  viewer.setState(twoClipState())
  viewer.render()
  viewer.seek(999)
  assert.deepEqual(visibleInspectorIds(elements), ['clip_one'])
  viewer.seek(1000)
  assert.deepEqual(visibleInspectorIds(elements), ['clip_two'])
  assert.match(elements.get('selected-clip-label').textContent, /^Clip 2 of 2(?: ·|$)/)
  viewer.seek(2000)
  assert.deepEqual(visibleInspectorIds(elements), ['clip_two'])
  viewer.seek(0)
  assert.deepEqual(visibleInspectorIds(elements), ['clip_one'])
})

test('the single zoom slider and keyboard timeline navigation remain usable without duplicate buttons', () => {
  const { viewer, elements } = viewerHarness()
  viewer.setState(twoClipState())
  viewer.render()
  const viewport = elements.get('visual-timeline')
  assert.equal(viewport.tagName, 'DIV')
  assert.equal(viewport.getAttribute('tabindex'), '0')
  assert.ok(viewport.events.has('keydown'))
  const initialWidth = elements.get('timeline-track').querySelector('.timeline-clip').style.width
  const zoom = elements.get('timeline-zoom')
  assert.equal(zoom.tagName, 'INPUT')
  assert.equal(zoom.getAttribute('type'), 'range')
  zoom.value = '192'
  zoom.dispatch('input')
  assert.equal(parseFloat(elements.get('timeline-track').querySelector('.timeline-clip').style.width),
    parseFloat(initialWidth) * 2, 'the remaining slider updates real clip geometry')
  elements.get('timeline-ruler').dispatch('click', { clientX: 288 })
  assert.equal(elements.get('playhead-time').textContent, '00:01.500')
  assert.deepEqual(visibleInspectorIds(elements), ['clip_two'])

  viewport.dispatch('keydown', { key: 'End' })
  assert.equal(elements.get('playhead-time').textContent, '00:02.000')
  assert.deepEqual(visibleInspectorIds(elements), ['clip_two'])
  viewport.dispatch('keydown', { key: 'Home' })
  assert.equal(elements.get('playhead-time').textContent, '00:00.000')
  viewport.dispatch('keydown', { key: 'ArrowRight', shiftKey: true })
  assert.equal(elements.get('playhead-time').textContent, '00:01.000')
  assert.deepEqual(visibleInspectorIds(elements), ['clip_two'])
  viewport.dispatch('keydown', { key: 'ArrowLeft' })
  assert.equal(elements.get('playhead-time').textContent, '00:00.900')
  assert.deepEqual(visibleInspectorIds(elements), ['clip_one'])
  const timelineClips = elements.get('timeline-track').querySelectorAll('.timeline-clip')
  timelineClips[1].querySelector('.timeline-clip-body').dispatch('click')
  assert.deepEqual(visibleInspectorIds(elements), ['clip_two'])
  timelineClips[0].querySelector('.timeline-clip-body').dispatch('click')
  assert.deepEqual(visibleInspectorIds(elements), ['clip_one'])
  assert.equal(elements.get('clips').children.length, 2)
})

test('a pending structural preview selects the displayed child inspector and keeps edits disabled', () => {
  const { viewer, elements } = viewerHarness()
  const state = twoClipState('video')
  viewer.setState(state)
  viewer.render()
  viewer.seek(250)

  const canonicalClips = state.projection.timeline.tracks[0].clips
  state.pending = splitPreviewState(state).pending
  viewer.render()
  assert.deepEqual(visibleInspectorIds(elements), ['pending_split_left'],
    'the split preview replaces the selected canonical id with a visible child inspector')
  viewer.seek(500)
  assert.deepEqual(visibleInspectorIds(elements), ['pending_split_right'])
  assert.match(elements.get('selected-clip-label').textContent, /^Clip 2 of 3(?: ·|$)/)
  assert.deepEqual(elements.get('clips').children.map(card => card.dataset.clipId),
    ['pending_split_left', 'pending_split_right', 'clip_two'])
  const editControls = elements.get('clips').querySelectorAll('button, input, select')
  assert.ok(editControls.length > 0, 'the actual pending inspector forms are retained')
  assert.ok(editControls.every(control => control.disabled),
    'switching the inspector cannot unlock pending edit controls')
  assert.equal(elements.get('pending').hidden, false)
  assert.equal(elements.get('save').disabled, false)
  assert.equal(elements.get('discard').disabled, false)
  assert.equal(elements.get('source-image').hidden, true)
  assert.deepEqual(canonicalClips.map(clip => clip.clip_id), ['clip_one', 'clip_two'],
    'view selection never rewrites the canonical timeline')
})

test('when a saved head removes the selected clip, rendering leaves one valid inspector visible', () => {
  const { viewer, elements } = viewerHarness()
  const state = twoClipState()
  viewer.setState(state)
  viewer.render()
  viewer.seek(1000)
  assert.deepEqual(visibleInspectorIds(elements), ['clip_two'])

  const newHead = twoClipState()
  newHead.projection.timeline_head.revision = 2
  newHead.projection.timeline.output.duration_ms = 1000
  newHead.projection.timeline.tracks[0].clips.pop()
  viewer.setState(newHead)
  viewer.render()
  assert.deepEqual(visibleInspectorIds(elements), ['clip_one'])
  assert.match(elements.get('selected-clip-label').textContent, /^Clip 1 of 1(?: ·|$)/)
  assert.equal(elements.get('clips').children.length, 1)
})

test('a staged Split focuses Save, then Save or Discard returns focus to the current timeline clip', () => {
  for (const outcome of ['save', 'discard']) {
    const { viewer, elements, document } = viewerHarness()
    const state = twoClipState('video')
    viewer.setState(state)
    viewer.render()
    viewer.seek(500)
    const split = elements.get('clips').querySelector('.split-clip')
    assert.equal(split.disabled, false)
    split.focus()
    viewer.setBusy(true)
    assert.equal(document.activeElement, document.body, 'rerender detaches the original Split button')

    const pendingState = splitPreviewState(state)
    viewer.setState(pendingState)
    viewer.setBusy(false)
    assert.equal(document.activeElement, elements.get('save'))
    elements.get(outcome).focus()
    viewer.setBusy(true)
    const completed = twoClipState('video')
    if (outcome === 'save') {
      completed.projection.timeline_head.revision = 2
      completed.projection.timeline.tracks[0].clips = pendingState.pending.preview.timeline.tracks[0].clips
        .map(clip => ({ ...clip, clip_id: clip.clip_id.replace('pending_split_', 'saved_split_') }))
    }
    viewer.setState(completed)
    viewer.setBusy(false)
    const selected = elements.get('timeline-track').querySelectorAll('.timeline-clip')
      .find(node => node.dataset.selected === 'true')
    assert.equal(document.activeElement, selected.querySelector('.timeline-clip-body'),
      `${outcome} restores focus to the newly rendered selected clip`)
    assert.deepEqual(visibleInspectorIds(elements), [selected.dataset.clipId])
  }
})

test('finishing a clip action preserves focus that the user moved to another input', () => {
  const { viewer, elements, document } = viewerHarness()
  const state = twoClipState('video')
  viewer.setState(state)
  viewer.render()
  viewer.seek(500)
  elements.get('clips').querySelector('.split-clip').focus()
  viewer.setBusy(true)
  const otherInput = document.createElement('input')
  document.body.append(otherInput)
  otherInput.focus()
  viewer.setState(splitPreviewState(state))
  viewer.setBusy(false)
  assert.equal(document.activeElement, otherInput)
  assert.deepEqual(visibleInspectorIds(elements), ['pending_split_right'])
})

test('an inspector with no eligible replacement keeps the reason in Source identity without an empty control', () => {
  const { viewer, elements } = viewerHarness()
  viewer.setState(twoClipState())
  viewer.render()
  for (const card of elements.get('clips').children) {
    assert.equal(card.querySelector('.replacement-review'), null)
    const identity = card.querySelector('.clip-meta')
    assert.equal(identity.tagName, 'DETAILS')
    assert.equal(identity.open, false)
    assert.match(identity.querySelector('.empty').textContent, /No verified, long-enough alternative/)
    assert.equal(card.querySelector('.precise-controls').tagName, 'DETAILS',
      'removing an empty replacement group retains the exact timing controls')
    assert.ok(card.querySelector('.structure-controls'))
  }
})

test('an eligible replacement retains verified comparison media and stages only the authorized closed edit', async () => {
  const state = twoClipState()
  const candidate = {
    assignment_id: 'assignment_alternative', media_kind: 'image',
    available_duration_ms: null, current_slot_duration_ms: 1000,
  }
  state.projection.replacement_candidates = { clip_one: [candidate] }
  state.capabilities.replacement_thumbnail = true
  const edit = { op: 'replace_clip', clip_id: 'clip_one', assignment_id: candidate.assignment_id }
  const requests = []
  const { viewer, elements } = viewerHarness({ fetch: async (url, options) => {
    requests.push({ url, options })
    return { ok: true, json: async () => ({
      ...state, pending: { object: 'memolens.canonical_editor_pending_edit', edit },
    }) }
  } })
  viewer.setState(state)
  viewer.render()
  const card = elements.get('clips').children[0]
  const replacement = card.querySelector('.replacement-review')
  assert.equal(replacement.tagName, 'DETAILS')
  assert.equal(replacement.open, false)
  const comparison = replacement.querySelectorAll('.visual-review-card')
  assert.equal(comparison.length, 2)
  assert.equal(comparison[0].dataset.current, 'true')
  assert.match(comparison[0].querySelector('img').src, /\/previews\/clip_one\/1$/)
  assert.match(comparison[1].querySelector('img').src,
    /\/replacement-previews\/clip_one\/assignment_alternative\/1$/)
  const stage = comparison[1].querySelector('button')
  assert.equal(stage.disabled, false)
  await stage.dispatch('click')
  assert.equal(requests.length, 1)
  assert.equal(requests[0].url, '/api/sessions/canonical_test/actions')
  assert.equal(requests[0].options.method, 'POST')
  assert.equal(requests[0].options.credentials, 'same-origin')
  assert.deepEqual(JSON.parse(requests[0].options.body), { type: 'stage', edit })
  const staged = elements.get('clips').children[0].querySelector('.replacement-candidates')
    .querySelector('.visual-review-card')
  assert.equal(staged.dataset.staged, 'true')
  assert.equal(staged.querySelector('button').disabled, true,
    'a pending replacement does not unlock a second edit')
  assert.equal(elements.get('save').disabled, false)
  assert.deepEqual(state.projection.timeline.tracks[0].clips.map(clip => clip.clip_id), ['clip_one', 'clip_two'])

  viewer.setState({ ...state, capabilities: { ...state.capabilities, replacement_thumbnail: false } })
  viewer.render()
  assert.equal(elements.get('clips').children[0].querySelector('.replacement-candidates').querySelector('img'), null,
    'thumbnail evidence still requires its own capability')
  viewer.setState({ ...state, capabilities: { ...state.capabilities, move_clip: false } })
  viewer.render()
  assert.equal(elements.get('clips').querySelector('.replacement-review'), null,
    'candidate presence cannot grant replacement edit authority')
})

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

test('the single Play replays from the end and Pause still stops playback without unlocking pending or history', () => {
  for (const kind of ['image', 'video']) {
    const { viewer, elements, frames } = viewerHarness()
    const state = currentState(kind)
    viewer.setState(state)
    viewer.sync()
    const play = elements.get('play-pause')
    play.dispatch('click')
    if (kind === 'image') {
      const tick = [...frames.values()].at(-1)
      frames.clear()
      tick(1200)
    } else {
      elements.get('source-video').currentTime = 1.2
      elements.get('source-video').dispatch('timeupdate')
    }
    assert.equal(viewer.paused(), true)
    assert.equal(elements.get('playhead-time').textContent, '00:01.000')
    play.dispatch('click')
    assert.equal(elements.get('playhead-time').textContent, '00:00.000', `${kind} replay seeks to the beginning`)
    assert.equal(viewer.paused(), false)
    assert.equal(play.textContent, 'Pause')
    if (kind === 'video') assert.equal(elements.get('source-video').currentTime, 0.2,
      'video replay seeks to the verified source-in, not to source zero')
    play.dispatch('click')
    assert.equal(viewer.paused(), true)
    assert.equal(play.textContent, 'Play')
    assert.equal(frames.size, 0)

    for (const mode of ['pending', 'history']) {
      const blocked = currentState(kind)
      if (mode === 'pending') blocked.pending = {
        object: 'memolens.canonical_editor_pending_edit', edit: { op: 'replace_clip' },
      }
      else blocked.historical = { selected_revision: { revision: 1 } }
      viewer.setState(blocked)
      viewer.seek(1000)
      assert.equal(play.disabled, true)
      play.dispatch('click')
      assert.equal(viewer.paused(), true, `${mode} cannot start ${kind} replay`)
      assert.equal(play.disabled, true)
      assert.equal(elements.get('playhead-time').textContent, '00:01.000',
        `${mode} does not rewind into a current-head source`)
      assert.equal(elements.get('source-image').hidden, true)
      assert.equal(elements.get('source-video').hidden, true)
    }
  }
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
