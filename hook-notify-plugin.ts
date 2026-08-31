import type { Plugin } from "@opencode-ai/plugin"

const SCRIPT =
  process.env.OCBRIDGE_NOTIFY_SCRIPT ||
  "/home/andrey/git/opencode-email-bridge/hook_notify.py"

const fs = await import("node:fs/promises")
const os = await import("node:os")
const path = await import("node:path")

function isText(part: any): part is { type: "text"; text: string } {
  return part && part.type === "text" && typeof part.text === "string"
}

/**
 * Fetch the session's name (title). Fall back to null on any failure.
 */
async function fetchSessionName(client: any, sessionId: string): Promise<string | null> {
  try {
    const result = await client.session.get({ path: { id: sessionId } })
    const sess =
      result && typeof result === "object" && !Array.isArray(result)
        ? result.data?.value ?? result.data ?? result
        : result
    const title = sess?.title || sess?.data?.title
    return typeof title === "string" && title.trim() ? title.trim() : null
  } catch (err) {
    console.error("[EmailNotifyPlugin] fetchSessionName failed:", err)
    return null
  }
}

/**
 * Fetch the last assistant (task-complete) output for a session using the
 * opencode SDK client. Gracefully returns null on any failure.
 */
async function fetchFinalOutput(client: any, sessionId: string): Promise<string | null> {
  try {
    const result = await client.session.messages({
      path: { id: sessionId },
      query: { limit: 10 },
    })
    // The SDK client may resolve to the array directly, or to { data, ... }.
    let msgs = result
    if (result && typeof result === "object" && !Array.isArray(result)) {
      msgs = result.data?.value ?? result.data
    }
    if (!Array.isArray(msgs)) return null

    // Iterate reversed so we pick up the last meaningful assistant reply.
    for (let i = msgs.length - 1; i >= 0; i--) {
      const entry = msgs[i]
      if (!entry || entry.info?.role !== "assistant") continue
      const text = (entry.parts ?? [])
        .filter(isText)
        .map((p) => p.text)
        .join("\n")
        .trim()
      if (text) return text
    }
    return null
  } catch (err) {
    console.error("[EmailNotifyPlugin] fetchFinalOutput failed:", err)
    return null
  }
}

export const EmailNotifyPlugin: Plugin = async ({ project, client, $, directory, worktree }) => {
  return {
    event: async ({ event }) => {
      const shouldNotify =
        event.type === "session.idle" ||
        event.type === "session.completed" ||
        event.type === "session.error"
      if (!shouldNotify) return

      const sessionId = event.properties?.sessionID || "unknown"
      // Prefer the real session name over the event's (often "Unknown Task").
      const sessionTitle =
        (await fetchSessionName(client, sessionId)) ||
        event.properties?.title ||
        "Unknown Task"
      const finalOutput = await fetchFinalOutput(client, sessionId)

      try {
        const exists = await fs
          .access(SCRIPT)
          .then(() => true)
          .catch(() => false)
        if (!exists) {
          console.warn(`[EmailNotifyPlugin] script not found, skipping: ${SCRIPT}`)
          return
        }

        // Pass the task output via stdin so arg-length is not a concern.
        const tmp = path.join(os.tmpdir(), `ocb-${sessionId}-${process.pid}.txt`)
        if (finalOutput) await fs.writeFile(tmp, finalOutput)

        // .quiet(): keep the script's stderr/stdout out of the TUI. The script
        // logs to its own file, so nothing needs to reach the terminal.
        await $`python3 ${SCRIPT} ${sessionId} ${sessionTitle} ${finalOutput ? tmp : ""}`.quiet()

        if (finalOutput) await fs.unlink(tmp).catch(() => {})
      } catch (err) {
        console.error("[EmailNotifyPlugin] hook_notify.py failed:", err)
      }
    },
  }
}
