from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class DeepSeekHarnessBundleTests(unittest.TestCase):
    def test_manifest_declares_installable_bundle_without_a_second_runtime(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "dsh-memolens")
        self.assertEqual(manifest["version"], "0.10.1")
        self.assertNotIn("dependencies", manifest)
        self.assertEqual(manifest["dsh"]["bundle"]["patch"], "./cordis.patch.yml")
        self.assertEqual(manifest["dsh"]["client"]["platform"], "web")
        self.assertEqual(manifest["exports"]["./client"], "./client.js")
        self.assertTrue(
            {
                "scripts/*.py",
                "ui",
                "skills",
                "deepseek-harness",
                "client.js",
            }.issubset(manifest["files"])
        )

    def test_patch_mounts_the_shared_mcp_through_harness_native_bridge(self) -> None:
        patch = (PLUGIN_ROOT / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertIn("name: dsh-memolens", patch)
        self.assertNotIn("./node_modules/dsh-memolens", patch)
        self.assertIn("name: '@deepseek-ai/dsh-mcp-client'", patch)
        self.assertIn("inject: [memolensBundle]", patch)
        self.assertIn("serverName: memolens", patch)
        self.assertIn("transport: stdio", patch)
        self.assertIn("ctx.memolensBundle.mcpScript", patch)
        self.assertIn("ctx.memolensBundle.root", patch)
        self.assertIn("failOnStartupError: true", patch)
        self.assertIn("MEMOLENS_DB_PATH:", patch)
        self.assertIn("name: '@deepseek-ai/dsh-skill-filesystem'", patch)
        self.assertIn("providerName: memolens-plugin", patch)
        self.assertIn("ctx.memolensBundle.deepseekSkills", patch)
        self.assertIn("name: dsh-memolens/prompt", patch)
        self.assertNotIn("/Users/", patch)
        self.assertTrue((PLUGIN_ROOT / "scripts" / "memolens_mcp.py").is_file())

    def test_root_provider_resolves_the_installed_package_at_runtime(self) -> None:
        module_url = (PLUGIN_ROOT / "index.js").as_uri()
        program = f"""
          import {{ apply }} from {json.dumps(module_url)};
          const values = [];
          apply({{ provide(name, value) {{ values.push({{ name, value }}); }} }});
          process.stdout.write(JSON.stringify(values));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", program],
            check=True,
            capture_output=True,
            text=True,
        )
        values = json.loads(completed.stdout)
        self.assertEqual(values[0]["name"], "memolensBundle")
        self.assertEqual(Path(values[0]["value"]["root"]), PLUGIN_ROOT)
        self.assertEqual(
            Path(values[0]["value"]["mcpScript"]),
            PLUGIN_ROOT / "scripts" / "memolens_mcp.py",
        )
        self.assertEqual(
            Path(values[0]["value"]["deepseekSkills"]),
            PLUGIN_ROOT / "deepseek-harness" / "skills",
        )

    def test_prompt_requires_a_clickable_handoff_without_false_auto_open_claim(self) -> None:
        module_url = (PLUGIN_ROOT / "prompt.js").as_uri()
        program = f"""
          import {{ apply }} from {json.dumps(module_url)};
          let section;
          apply({{ systemPrompt: {{ section(value) {{ section = value; }} }} }});
          process.stdout.write(JSON.stringify(section));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", program],
            check=True,
            capture_output=True,
            text=True,
        )
        section = json.loads(completed.stdout)
        text = section["text"]
        self.assertEqual(section["name"], "tool:memolens")
        self.assertIn(
            "mcp__memolens__memolens_canonical_editor_handoff", text
        )
        self.assertIn(
            "mcp__memolens__memolens_library_bootstrap_start", text
        )
        self.assertIn(
            "mcp__memolens__memolens_library_bootstrap_status", text
        )
        self.assertIn("tell the user to open MemoLens themselves", text)
        self.assertIn("library_authority_committed", text)
        self.assertIn("mcp__memolens__memolens_status", text)
        self.assertIn("database.library_scan", text)
        self.assertIn("immutable bootstrap receipt", text)
        self.assertIn("does not independently establish", text)
        self.assertIn("mcp__memolens__memolens_editor_handoff", text)
        self.assertIn("Open MemoLens Canonical Editor", text)
        self.assertIn("uiHandoff.url", text)
        self.assertIn("user gesture", text)
        self.assertIn("Not saved", text)
        self.assertIn("timeline.preview_media", text)
        self.assertIn("Raw MP4 preview transport may contain audio", text)
        self.assertIn("disables audio playback and keeps output muted", text)
        self.assertIn("Audio-stream presence, content, and mix are not attested", text)
        self.assertIn("never claim that the editor opened automatically", text)
        self.assertNotIn("automatically opens", text)

    def test_client_registers_exact_user_gesture_only_editor_buttons(self) -> None:
        client_path = PLUGIN_ROOT / "client.js"
        canonical_sample = {
            "uiHandoff": {
                "url": "http://127.0.0.1:43123/canonical-editor/canonical_demo#boot=secret"
            }
        }
        draft_sample = {
            "uiHandoff": {
                "url": "http://127.0.0.1:43123/editor/edit_demo#boot=secret"
            }
        }
        program = f"""
          let handoff;
          globalThis.window = {{ __ModuleLoader__: {{ load(value) {{ handoff = value; }} }} }};
          globalThis.opened = [];
          globalThis.open = (...args) => {{ opened.push(args); return {{ opener: 'set' }}; }};
          await import({json.dumps(client_path.as_uri())});
          const React = {{ createElement(type, props, ...children) {{ return {{ type, props: props ?? {{}}, children }}; }} }};
          const plugin = handoff.factory((name) => {{ if (name === 'react') return React; throw new Error(name); }});
          const registrations = [];
          plugin.apply({{ slots: {{ inject(_name, mount) {{ mount(); }}, register(options, component) {{ registrations.push({{ options, component }}); return () => {{}}; }} }} }});
          const canonical = registrations.find(item => item.options.key.endsWith('canonical_editor_handoff'));
          const draft = registrations.find(item => item.options.key.endsWith('memolens_editor_handoff'));
          const canonicalBlock = {{
            kind: 'tool-result', isError: false, content: [{{ type: 'text', text: {json.dumps(json.dumps(canonical_sample))} }}]
          }};
          const draftBlock = {{
            kind: 'tool-result', isError: false, content: [{{ type: 'text', text: {json.dumps(json.dumps(draft_sample))} }}]
          }};
          const canonicalTree = canonical.component({{ block: canonicalBlock }});
          const draftTree = draft.component({{ block: draftBlock }});
          const canonicalButton = canonicalTree.children.find(child => child.type === 'button');
          const draftButton = draftTree.children.find(child => child.type === 'button');
          canonicalButton.props.onClick({{ stopPropagation() {{}} }});
          process.stdout.write(JSON.stringify({{
            id: handoff.id,
            inject: plugin.inject,
            keys: registrations.map(item => item.options.key),
            canonicalButton: canonicalButton.children[0],
            draftButton: draftButton.children[0],
            opened,
          }}));
        """
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", program],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["id"], "dsh-memolens")
        self.assertEqual(result["inject"], ["slots"])
        self.assertEqual(result["keys"], [
            "mcp__memolens__memolens_canonical_editor_handoff",
            "mcp__memolens__memolens_editor_handoff",
        ])
        self.assertEqual(
            result["canonicalButton"], "Open MemoLens Canonical Editor"
        )
        self.assertEqual(
            result["draftButton"], "Open MemoLens Unsaved Draft Lab"
        )
        self.assertEqual(
            result["opened"][0][0], canonical_sample["uiHandoff"]["url"]
        )
        self.assertEqual(result["opened"][0][1:], ["_blank", "noopener,noreferrer"])

    def test_deepseek_skill_uses_harness_names_and_keeps_the_boundary(self) -> None:
        skill = (
            PLUGIN_ROOT
            / "deepseek-harness"
            / "skills"
            / "use-memolens"
            / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("name: use-memolens", skill)
        self.assertIn("mcp__memolens__memolens_canonical_editor_handoff", skill)
        self.assertIn("mcp__memolens__memolens_library_bootstrap_start", skill)
        self.assertIn("mcp__memolens__memolens_library_bootstrap_status", skill)
        self.assertIn("library_authority_committed", skill)
        self.assertIn("mcp__memolens__memolens_status", skill)
        self.assertIn("database.library_scan", skill)
        self.assertIn("receipt-anchored", skill)
        self.assertIn("newer/rogue scan row", skill)
        self.assertIn("mcp__memolens__memolens_editor_handoff", skill)
        self.assertIn("Open MemoLens Canonical Editor", skill)
        self.assertIn("Open MemoLens Unsaved Draft Lab", skill)
        self.assertIn("uiHandoff.url", skill)
        self.assertIn("Not saved", skill)
        self.assertIn("timeline.preview_media", skill)
        self.assertIn("Play/Pause/seek", skill)
        self.assertNotIn("Codex's in-app Browser", skill)

        guide = (PLUGIN_ROOT / "deepseek-harness" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Loader and focused local validation do not prove", guide)
        self.assertIn("a fresh DeepSeek model call", guide)
        self.assertIn("host-acceptance gates", guide)


if __name__ == "__main__":
    unittest.main()
