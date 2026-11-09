import { execFile } from "node:child_process"
import { existsSync } from "node:fs"
import { join } from "node:path"
import { promisify } from "node:util"
import { Plugin } from "@opencode/plugin"

const exec = promisify(execFile)

export default Plugin.define({
  id: "fitagent-lint",
  async setup(ctx) {
    const directory = ctx.location.directory
    const localPython = join(directory, ".venv", "bin", "python3")
    const python = process.env.FITAGENT_PYTHON ?? (existsSync(localPython) ? localPython : "python3")

    // Run after every tool, including shell/code-mode tools that may change files.
    // This is feedback for the next step, NOT a blocking Stop hook.
    await ctx.tool.hook("execute.after", async (event) => {
      let report: string
      try {
        const result = await exec(python, [join(directory, "scripts", "lint.py")], {
          cwd: directory,
          timeout: 60_000,
          maxBuffer: 1024 * 1024,
        })
        report = result.stdout + result.stderr
      } catch (error) {
        const failure = error as Error & { stdout?: string; stderr?: string }
        report = `LINT FAILED\n${failure.stdout ?? ""}${failure.stderr ?? ""}\n${failure.message}`
      }
      await ctx.session.instructions.entry.put({
        sessionID: event.sessionID,
        key: "fitagent.lint",
        value: `Результат post-hook линтера:\n${report.slice(-16000)}\n` +
          "При LINT FAILED исправь ошибки и повтори проверку. Не объявляй задачу выполненной.",
      })
    })
  },
})
