"""自动唤醒：有人点名发消息给开着的 agent 时，叫它自己去 read。

- detector.py：感知模块。看有没有新消息、按规则决定该叫醒谁；只读写数据库，不管怎么叫醒。
- claude.py 等：适配器，每种 agent 一个，只管「怎么叫醒」。每个适配器提供
  check(agent) -> 叫不醒的原因或 None，和 wake(call)（失败抛异常）。
- service.py：把两者连起来的后台线程，跟着 kopitiam web 一起跑，也可以用 kopitiam wake 单独跑。

这里不导入任何子模块：hook 进程只加载它要用的那一个适配器，启动快。
"""
