/**
 * 扫描状态轮询：提交扫描后持续查询 /status，扫描一结束就回调一次 onDone，
 * 由页面自己刷新统计。用 setTimeout 串行轮询，避免慢请求叠在一起。
 *
 * @param {object} api        插件宿主注入的 api（get/post）
 * @param {string} pluginId   插件 ID
 * @param {object} opts
 * @param {(res:any)=>void} [opts.onTick]  每次拿到状态时回调
 * @param {(res:any)=>void} [opts.onDone]  扫描结束（或超时）时回调一次
 * @param {(err:any)=>void} [opts.onError] 某次查询失败时回调
 * @param {number} [opts.interval=3000]   轮询间隔毫秒
 * @param {number} [opts.maxMs=1800000]   最长等待，30 分钟，防止大库无限转圈
 * @returns {{cancel:()=>void, done:boolean}} 可取消的句柄
 */
export function watchScan(api, pluginId, opts = {}) {
  const { onTick, onDone, onError, interval = 3000, maxMs = 30 * 60 * 1000 } = opts;
  const started = Date.now();
  const handle = { cancelled: false, finished: false, timer: null };

  const finish = (res) => {
    if (handle.finished || handle.cancelled) return;
    handle.finished = true;
    onDone && onDone(res);
  };

  const tick = async () => {
    if (handle.cancelled) return;
    let res = null;
    try {
      res = await api.get(`plugin/${pluginId}/status`);
      onTick && onTick(res);
    } catch (e) {
      onError && onError(e);
    }
    if (handle.cancelled) return;
    // 后端未返回 scanning 字段（旧版本）时视为已结束，退化为「拉一次就刷新」
    const running = !!(res && res.scanning);
    if (!running) {
      finish(res);
      return;
    }
    if (Date.now() - started > maxMs) {
      finish(res);
      return;
    }
    handle.timer = setTimeout(tick, interval);
  };

  handle.cancel = () => {
    handle.cancelled = true;
    if (handle.timer) clearTimeout(handle.timer);
  };
  tick();
  return handle;
}
