window.__ModuleLoader__.load({
  id: 'dsh-memolens',
  factory: (require) => {
    'use strict'

    const { createElement } = require('react')
    const CANONICAL_TOOL = 'mcp__memolens__memolens_canonical_editor_handoff'
    const DRAFT_TOOL = 'mcp__memolens__memolens_editor_handoff'

    function resultPayload(block) {
      if (!Object.prototype.hasOwnProperty.call(block, 'kind')) return null
      for (const item of block.content ?? []) {
        if (item.type !== 'text') continue
        try {
          const value = JSON.parse(item.text)
          if (value !== null && typeof value === 'object') return value
        } catch {
          // Streaming and older results may contain non-JSON text. Keep an
          // honest failure card instead of guessing or rewriting a URL.
        }
      }
      return null
    }

    function editorUrl(block, canonical) {
      if (block.isError) return null
      const payload = resultPayload(block)
      const raw = payload?.uiHandoff?.url
      if (typeof raw !== 'string') return null
      try {
        const url = new URL(raw)
        const pathPattern = canonical
          ? /^\/canonical-editor\/canonical_[A-Za-z0-9_-]+$/
          : /^\/editor\/edit_[A-Za-z0-9_-]+$/
        if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1') return null
        if (url.username !== '' || url.password !== '') return null
        if (url.port === '' || url.search !== '') return null
        if (!pathPattern.test(url.pathname) || !/^#boot=[A-Za-z0-9_-]+$/.test(url.hash)) return null
        // Parse only to validate. The host must open the exact tool-result URL,
        // byte-for-byte, without normalizing or reconstructing it.
        return raw
      } catch {
        return null
      }
    }

    function handoffFailureMessage(block) {
      // Only a known code selects trusted copy. Error messages, paths and URLs
      // from tool output never become recovery instructions or open targets.
      switch (resultPayload(block)?.error?.code) {
        case 'canonical_editor_not_editable':
          return 'No editable canonical Timeline is ready. Finish grounded Coverage and Timeline creation for this project, then retry the editor handoff.'
        case 'canonical_editor_not_current':
        case 'canonical_editor_evidence_non_current':
          return 'Refresh this project and rebuild current Coverage and Timeline evidence, then retry the editor handoff.'
        case 'agent_pairing_required':
        case 'agent_credential_unavailable':
        case 'agent_credential_invalid':
        case 'agent_credential_insecure':
          return 'Open MemoLens Desktop and review this project pairing. Approve or renew the pairing there, then retry the editor handoff.'
        case 'agent_pairing_scope_denied':
          return 'Open MemoLens Desktop and review this project pairing permission. Approve the needed Timeline action, then retry the editor handoff.'
        case 'service_unavailable':
          return 'Check that MemoLens Desktop is running with the intended Library and project, then retry the editor handoff.'
        case 'canonical_editor_project_required':
          return 'Select an existing canonical project and retry the handoff with its project_id.'
        case 'canonical_editor_database_mismatch':
        case 'service_identity_mismatch':
          return 'Open the intended Library and project in MemoLens Desktop, renew its pairing, then retry the editor handoff.'
        case 'canonical_editor_projection_invalid':
        case 'canonical_editor_projection_mismatch':
          return 'The canonical project could not be verified. Refresh its Coverage and Timeline in MemoLens Desktop, inspect the result, then retry the editor handoff.'
        default:
          return 'Editor handoff unavailable. Inspect the tool result and retry the handoff after resolving its reported state.'
      }
    }

    function editorRow({ block, inspect, canonical }) {
      const toolName = canonical ? CANONICAL_TOOL : DRAFT_TOOL
      const settled = Object.prototype.hasOwnProperty.call(block, 'kind')
      const url = settled ? editorUrl(block, canonical) : null
      const failed = settled && (block.isError || url === null)
      const title = canonical ? 'MemoLens Canonical Editor' : 'MemoLens Unsaved Draft Lab'
      const boundary = canonical ? 'Explicit Save creates N+1' : 'Not saved · process-scoped'
      const buttonLabel = canonical
        ? 'Open MemoLens Canonical Editor'
        : 'Open MemoLens Unsaved Draft Lab'

      const openEditor = (event) => {
        event.stopPropagation()
        const opened = globalThis.open(url, '_blank', 'noopener,noreferrer')
        if (opened !== null && typeof opened === 'object') opened.opener = null
      }

      const children = [
        createElement('strong', { key: 'title' }, title),
        createElement('span', { key: 'boundary' }, boundary),
      ]
      if (!settled) {
        children.push(createElement('span', { key: 'state' }, 'Preparing editor…'))
      } else if (failed) {
        children.push(createElement(
          'span',
          { key: 'state', role: 'status' },
          handoffFailureMessage(block),
        ))
      } else {
        children.push(createElement(
          'button',
          {
            key: 'open',
            type: 'button',
            onClick: openEditor,
            'aria-label': `${buttonLabel} in a new tab`,
          },
          buttonLabel,
        ))
      }
      if (inspect !== undefined) {
        children.push(createElement('button', {
          key: 'inspect',
          type: 'button',
          onClick: inspect,
        }, 'Inspect'))
      }
      return createElement('div', {
        'data-tool': toolName,
        'data-state': !settled ? 'running' : failed ? 'error' : 'ready',
      }, ...children)
    }

    function CanonicalEditorRow(props) {
      return editorRow({ ...props, canonical: true })
    }

    function DraftLabRow(props) {
      return editorRow({ ...props, canonical: false })
    }

    const inject = ['slots']
    function apply(ctx) {
      ctx.slots.inject('tool.call.toolview', () => {
        const unregisterCanonical = ctx.slots.register(
          { name: 'tool.call.toolview', key: CANONICAL_TOOL },
          CanonicalEditorRow,
        )
        const unregisterDraft = ctx.slots.register(
          { name: 'tool.call.toolview', key: DRAFT_TOOL },
          DraftLabRow,
        )
        return () => {
          if (typeof unregisterCanonical === 'function') unregisterCanonical()
          if (typeof unregisterDraft === 'function') unregisterDraft()
        }
      })
    }

    return { apply, inject }
  },
})
