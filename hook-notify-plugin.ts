import type { Plugin } from "@opencode-ai/plugin"

const SCRIPT = "/home/andrey/opencode-email-bridge/hook_notify.py"

const fs = await import("node:fs/promises")

export const EmailNotifyPlugin: Plugin = async ({ project, client, $, directory, worktree }) => {
  return {
    event: async ({ event }) => {
      if (event.type !== "session.idle") return

      const sessionId = event.properties?.sessionID || "unknown"
      const sessionTitle = event.properties?.title || "Unknown Task"

      try {
        const exists = await fs
          .access(SCRIPT)
          .then(() => true)
          .catch(() => false)
        if (!exists) {
          console.warn(`[EmailNotifyPlugin] script not found, skipping: ${SCRIPT}`)
          return
        }

        await $`python3 ${SCRIPT} ${sessionId} ${sessionTitle}`
      } catch (err) {
        console.error("[EmailNotifyPlugin] hook_notify.py failed:", err)
      }
    },
  }
}
