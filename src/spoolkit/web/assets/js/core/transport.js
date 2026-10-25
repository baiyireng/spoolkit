// spool web · js/core/transport.js
import "./dom.js";
// 事件流只连**一次**。切换目标**不重连**：服务端发现目标变了会退订旧的、
// 订阅新的、并补一帧 `state`（设计 §3.3），页面要做的只是把这一屏重绑到新目标上。
function createTransport(ctx) {
  function connect() {
    const source = new EventSource('/events');
    source.onmessage = onEvent;
    // 事件流断了之后，start / delta / final 都到不了，页面就停在自己那句话上。
    // 浏览器会自己重连；重连之前先按 /state 把已经写进库的回复或失败补上。
    source.onerror = () => {
      conn.style.display = 'block';
      if (source._poll) { return; }
      const pull = () => {
        fetch('/state').then((response) => response.json()).then((data) => {
          if (data && typeof applyState === 'function') { applyState(data); }
        }).catch(() => {});
      };
      pull();
      source._poll = setInterval(pull, 2000);
    };
    source.onopen = () => {
      conn.style.display = 'none';
      if (source._poll && typeof clearInterval === 'function') { clearInterval(source._poll); }
      source._poll = 0;
    };
  }

  // 一个 POST：成功返回响应体（`/target` 的响应体就是新的完整状态），
  // 失败**给出可读说明**：既摆在对话区里（记录），也**弹**一条只能手动关的错误通知
  // （不往控制台一丢了事：本地单机壳没人开着 devtools，"点了一下什么都没发生"
  // 是最坏的那种失败）。
  async function post(url, payload) {
    function rejectPost(why) {
      ctx.thread.add('请求被拒绝：' + why, 'bad');
      ctx.toasts.notifyFailure('请求被拒绝：', why, explainFailure(why, null));
      return null;
    }
    let response;
    try {
      response = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
    } catch (err) {
      return rejectPost('网络异常');
    }
    let detail;
    try {
      detail = await response.json();
    } catch (err) {
      return rejectPost('响应无法解析');
    }
    if (!response.ok) {
      return rejectPost(String((detail && detail.error) || response.status));
    }
    return detail;
  }

  return { connect: connect, post: post };
}
