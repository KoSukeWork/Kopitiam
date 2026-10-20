/**
 * kopitiam：dsh 这一边的插件（cordis）。
 * 由 `kopitiam setup dsh` 复制到 ~/.dsh/profiles/<profile>/kopitiam-wake-<哈希>.mjs，并写进那个 profile 的 cordis.patch.yml。
 * 文件都在 <KOPITIAM_HOME 或 ~/.kopitiam>/wake/ 里，用 dsh 的进程号区分（kopitiam 的 MCP 进程也是 dsh 启动的，对得上）。
 *
 * - 哪个会话在调用：一个 dsh 进程管着好几个会话，却只有一个 kopitiam MCP 进程，dsh 调用 MCP 工具时也不带会话信息。
 *   所以会话调用 kopitiam 的工具（mcp__kopitiam__*）时，这里先把会话 ID、工作目录、标题写进 dsh-<进程号>.calling.json，
 *   再真正去调用，kopitiam 收到请求时读这个文件认人（一个会话一个身份）。kopitiam 的调用在这里排队、一次一个，
 *   几个会话同时调用也不会认错（kopitiam 的工具都是几毫秒的事）。
 * - 怎么叫醒：kopitiam 要叫醒某个会话时写 dsh-<进程号>.wake-<叫醒编号>.json（{id, text, session}）。这里看到就用
 *   sessionController.prompt 发进那个会话，和在网页上发消息一样：会话闲着马上开始，正在忙就排在这一轮后面；
 *   然后删掉文件。requestId 用叫醒编号，dsh 自己保证同一个只收一次。
 * - dsh-<进程号>.signal 在 = 装好了这个插件（kopitiam 那边据此判断能不能叫醒）。
 */
import { existsSync, mkdirSync, readFileSync, unlinkSync, watch, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const name = "kopitiam-wake";
export const inject = ["sessionController"];

export function apply(ctx) {
  const dir = join(process.env.KOPITIAM_HOME || join(homedir(), ".kopitiam"), "wake");
  const prefix = `dsh-${process.pid}`;
  const signal = join(dir, `${prefix}.signal`);
  const calling = join(dir, `${prefix}.calling.json`);
  let queue = Promise.resolve(); // kopitiam 的调用排队，一次一个
  const timers = new Map();

  mkdirSync(dir, { recursive: true });
  if (!existsSync(signal)) writeFileSync(signal, ""); // 装好了：可以叫醒

  const titleOf = (session) => {
    try {
      return ctx.get?.("sessionTitle")?.get(session)?.title ?? ""; // 没有标题服务的 dsh 也能用
    } catch {
      return "";
    }
  };

  ctx.on("tools/execute", (exec, next) => {
    const session = exec.agent?.session;
    const id = session?.header?.id;
    if (!exec.name.startsWith("mcp__kopitiam__") || !id) return next();
    const run = queue.then(async () => {
      writeFileSync(calling, JSON.stringify({ session: id, cwd: session.header.cwd ?? "", title: titleOf(session) }));
      try {
        return await next();
      } finally {
        remove(calling);
      }
    });
    queue = run.catch(() => {});
    return run;
  });

  // 发给指定的会话：读到就删，同一次叫醒只发一次
  const deliver = async (file) => {
    let msg;
    try {
      msg = JSON.parse(read(join(dir, file)));
    } catch {
      return; // kopitiam 写完才改成这个名字，读不出来就是已经发过、删掉了
    }
    remove(join(dir, file));
    if (!msg?.id || !msg.text || !msg.session) return;
    try {
      // 第二个参数 signal 是必填的（prompt 一开始就调用 signal.throwIfAborted()）
      await ctx.sessionController.prompt({
        requestId: `kopitiam-${msg.id}`,
        sessionId: msg.session,
        mode: "queue",
        content: [{ type: "text", text: msg.text }],
      }, AbortSignal.timeout(60_000));
    } catch (error) {
      ctx.logger.warn(`kopitiam-wake: 发不进会话 ${msg.session}：${String(error)}`);
    }
  };

  ctx.effect(() => {
    mkdirSync(dir, { recursive: true });
    // 监视文件夹：一次写入可能触发好几次事件，合并成一次
    const watcher = watch(dir, (_event, changed) => {
      if (!changed?.startsWith(`${prefix}.wake-`) || !changed.endsWith(".json")) return;
      clearTimeout(timers.get(changed));
      timers.set(changed, setTimeout(() => { timers.delete(changed); deliver(changed); }, 80));
    });
    return () => {
      watcher.close();
      for (const t of timers.values()) clearTimeout(t);
      remove(signal);
      remove(calling);
    };
  }, "kopitiam-wake: watch the wake signal");
}

function read(path) {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return "";
  }
}

function remove(path) {
  try {
    unlinkSync(path);
  } catch {
    // 已经不在了
  }
}
