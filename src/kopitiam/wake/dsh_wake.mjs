/**
 * kopitiam 自动唤醒：dsh 这一边的接收端（cordis 插件）。
 * 由 `kopitiam setup dsh` 复制到 ~/.dsh/profiles/<profile>/kopitiam-wake.mjs，并写进那个 profile 的 cordis.patch.yml。
 *
 * - 哪个会话：一个 dsh 进程管着好几个会话。会话调用 kopitiam 的工具（mcp__kopitiam__*）时记下它的会话 ID，
 *   叫醒时就发给这个会话。最近的会话 ID 也存进文件，dsh 重开以后还能用。
 * - 怎么叫醒：kopitiam 往 <KOPITIAM_HOME 或 ~/.kopitiam>/wake/dsh-<进程号>.signal 写 {"id", "text"}；
 *   这里读到新的 id，就用 sessionController.prompt 把提示当成一条用户消息发进那个会话，和在网页上发消息一样：
 *   会话闲着就马上开始，正在忙就排在这一轮后面。requestId 用叫醒编号，dsh 自己保证同一个只收一次。
 * - 信号文件在 = 装好了、并且知道发给哪个会话（kopitiam 那边据此判断能不能叫醒）。
 */
import { mkdirSync, readFileSync, unlinkSync, watch, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const name = "kopitiam-wake";
export const inject = ["sessionController"];

export function apply(ctx) {
  const dir = join(process.env.KOPITIAM_HOME || join(homedir(), ".kopitiam"), "wake");
  const signalName = `dsh-${process.pid}.signal`;
  const signal = join(dir, signalName);
  const remembered = join(dir, "dsh-session.txt");
  let session = read(remembered).trim() || undefined; // 最近用过 kopitiam 的会话
  let lastId = "";
  let timer;

  const ready = () => {
    mkdirSync(dir, { recursive: true });
    try {
      readFileSync(signal);
    } catch {
      writeFileSync(signal, ""); // 第一次知道会话：可以叫醒了
    }
  };
  if (session) ready();

  ctx.on("tools/post-execute", async (exec, result, next) => {
    const id = exec.agent?.session?.header?.id;
    if (exec.name.startsWith("mcp__kopitiam__") && id && id !== session) {
      session = id;
      mkdirSync(dir, { recursive: true });
      writeFileSync(remembered, id);
      ready();
    }
    return next();
  });

  const deliver = async () => {
    let msg;
    try {
      msg = JSON.parse(read(signal));
    } catch {
      return; // 空文件或者正在写：等下一次变化
    }
    if (!msg?.id || !msg.text || msg.id === lastId || !session) return;
    lastId = msg.id;
    try {
      // 第二个参数 signal 是必填的（prompt 一开始就调用 signal.throwIfAborted()）
      await ctx.sessionController.prompt({
        requestId: `kopitiam-${msg.id}`,
        sessionId: session,
        mode: "queue",
        content: [{ type: "text", text: msg.text }],
      }, AbortSignal.timeout(60_000));
    } catch (error) {
      ctx.logger.warn(`kopitiam-wake: 发不进会话 ${session}：${String(error)}`);
    }
  };

  ctx.effect(() => {
    mkdirSync(dir, { recursive: true });
    // 监视文件夹而不是文件本身：一次写入可能触发好几次事件，合并成一次
    const watcher = watch(dir, (_event, changed) => {
      if (changed !== signalName) return;
      clearTimeout(timer);
      timer = setTimeout(deliver, 80);
    });
    return () => {
      watcher.close();
      clearTimeout(timer);
      try {
        unlinkSync(signal);
      } catch {
        // 已经不在了
      }
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
