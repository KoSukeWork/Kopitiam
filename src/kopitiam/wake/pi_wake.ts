/**
 * kopitiam 自动唤醒：pi 这一边的接收端（由 `kopitiam setup pi` 复制到 ~/.pi/agent/extensions/）。
 *
 * kopitiam 要叫醒这个 pi 时，往 <KOPITIAM_HOME 或 ~/.kopitiam>/wake/pi-<进程号>.signal 写
 * {"id": 这次叫醒的编号, "text": 给 pi 的提示}。这里监视它，读到新的 id 就用 sendUserMessage
 * 交给 pi：空闲时马上开始一个回合，正在忙就排在这一轮后面。
 *
 * 用户自己在 pi 里打了字，就写 wake/pi-<进程号>.typed（{"at": 时间}），kopitiam 据此让这个 pi
 * 和别的 agent 之间的来回重新计数。扩展发的（包括上面交的唤醒提示）不算。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { type FSWatcher, mkdirSync, readFileSync, unlinkSync, watch, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export default function (pi: ExtensionAPI) {
  const dir = join(process.env.KOPITIAM_HOME || join(homedir(), ".kopitiam"), "wake");
  const name = `pi-${process.pid}.signal`;
  const file = join(dir, name);
  const typed = join(dir, `pi-${process.pid}.typed`);
  let watcher: FSWatcher | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let lastId = "";

  const deliver = () => {
    let msg: { id?: string; text?: string };
    try {
      msg = JSON.parse(readFileSync(file, "utf8"));
    } catch {
      return; // 空文件或者正在写：等下一次变化
    }
    if (!msg.id || !msg.text || msg.id === lastId) return;
    lastId = msg.id;
    pi.sendUserMessage(msg.text, { deliverAs: "followUp" });
  };

  pi.on("session_start", () => {
    if (watcher) return;
    mkdirSync(dir, { recursive: true });
    writeFileSync(file, ""); // 文件在 = 这个 pi 能被叫醒
    // 监视文件夹而不是文件本身：一次写入可能触发好几次事件，合并成一次
    watcher = watch(dir, (_event, changed) => {
      if (changed !== name) return;
      clearTimeout(timer);
      timer = setTimeout(deliver, 80);
    });
  });

  // source：interactive = 在 pi 里打的，rpc = 外面的界面发的，extension = 扩展发的（sendUserMessage）
  pi.on("input", (event) => {
    if (event.source === "extension" || !event.text.trim() || event.text.trimStart().startsWith("kopitiam：你有 ")) return;
    try {
      mkdirSync(dir, { recursive: true });
      writeFileSync(typed, JSON.stringify({ at: Date.now() / 1000 }));
    } catch {
      // 记不下也不影响用户发消息
    }
  });

  pi.on("session_shutdown", () => {
    watcher?.close();
    watcher = undefined;
    try {
      unlinkSync(file);
    } catch {
      // 已经不在了
    }
  });
}
