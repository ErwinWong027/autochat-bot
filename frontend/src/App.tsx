import { useEffect, useMemo, useState } from "react";
import heroCharacter from "./assets/hero-character.png";
import {
  AndroidRunPayload,
  AndroidStatus,
  BumbleStatus,
  ContactDetail,
  ContactSummary,
  DraftResult,
  createDraft,
  getAndroidStatus,
  getBumbleStatus,
  getContact,
  getContacts,
  startAndroid,
  startBumble,
  stopAndroid,
  stopBumble,
  type Overview
} from "./api/client";

type Route = "/about" | "/agent" | "/board";

const ANDROID_APPS_CONFIG = [
  { key: "wechat", label: "微信" },
  { key: "tantan", label: "探探" },
  { key: "momo", label: "陌陌" },
  { key: "red", label: "小红书" },
  { key: "qianshou", label: "千手" },
] as const;
type AndroidAppKey = typeof ANDROID_APPS_CONFIG[number]["key"];

const defaultOverview: Overview = {
  contact_count: 0,
  message_count: 0,
  reply_count: 0,
  sent_count: 0
};

function currentRoute(): Route {
  const path = window.location.pathname;
  if (path === "/agent") return "/agent";
  if (path === "/board") return "/board";
  return "/about";
}

function formatTime(value?: string) {
  if (!value) return "NO TIME";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function contactName(contact?: ContactSummary) {
  return contact?.display_name || contact?.contact_id || "UNKNOWN";
}

function shortContactId(contactId: string) {
  const clean = contactId.replace("bumble:", "");
  if (clean.length <= 14) return clean;
  return `${clean.slice(0, 6)}...${clean.slice(-4)}`;
}

function readableProfile(profile?: string, fallback?: string) {
  const raw = (profile || fallback || "").trim();
  if (!raw) return "暂无简介";
  const parts = raw
    .split("；")
    .map((item) => item.trim())
    .filter(Boolean)
    .filter((item) => !item.startsWith("photo_urls"))
    .filter((item) => !item.includes("https://"))
    .filter((item) => !item.includes("euri="));
  return (parts.slice(0, 4).join(" · ") || fallback || "暂无简介").slice(0, 160);
}

const fieldLabels: Record<string, string> = {
  age: "年龄",
  bio: "简介",
  dating_intentions: "关系意图",
  drinking: "饮酒",
  education: "教育",
  exercise: "运动",
  family_plans: "家庭计划",
  gender: "性别",
  height: "身高",
  hometown: "家乡",
  job: "工作",
  location: "位置",
  platform_name: "平台名",
  profile_prompts: "主页问答",
  zodiac: "星座",
  hobbies: "兴趣"
};

function readableFieldValue(field: string, value: string) {
  if (!value) return "";
  if (field === "profile_prompts") {
    try {
      const prompts = JSON.parse(value) as Array<{ title?: string; answer?: string }>;
      return prompts.map((item) => [item.title, item.answer].filter(Boolean).join(": ")).join(" / ");
    } catch {
      return value;
    }
  }
  return value.replace(/；/g, " · ").slice(0, 220);
}

function visibleProfileFields(detail: ContactDetail | null) {
  return Object.values(detail?.fields || {})
    .filter((field) => field.field !== "photo_urls")
    .filter((field) => !field.value.includes("https://"))
    .filter((field) => !field.value.includes("euri="))
    .slice(0, 12);
}

export default function App() {
  const [route, setRoute] = useState<Route>(currentRoute());
  const [overview, setOverview] = useState<Overview>(defaultOverview);
  const [contacts, setContacts] = useState<ContactSummary[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<ContactDetail | null>(null);
  const [status, setStatus] = useState<BumbleStatus | null>(null);
  const [targetUrl, setTargetUrl] = useState("https://eu1.bumble.com/app/connections");
  const [autoSend, setAutoSend] = useState(true);
  const [refreshProfile, setRefreshProfile] = useState(false);
  const [pollSeconds, setPollSeconds] = useState(5);
  const [draftInput, setDraftInput] = useState("");
  const [draftResult, setDraftResult] = useState<DraftResult | null>(null);
  const [error, setError] = useState("");
  const [selectedAndroidApp, setSelectedAndroidApp] = useState<AndroidAppKey>("tantan");
  const [androidAdbAddress, setAndroidAdbAddress] = useState("127.0.0.1:7555");
  const [androidAutoSend, setAndroidAutoSend] = useState(true);
  const [androidPollSeconds, setAndroidPollSeconds] = useState(8);
  const [androidStatuses, setAndroidStatuses] = useState<Record<AndroidAppKey, AndroidStatus | null>>(
    Object.fromEntries(ANDROID_APPS_CONFIG.map((a) => [a.key, null])) as Record<AndroidAppKey, AndroidStatus | null>
  );

  function navigate(nextRoute: Route) {
    window.history.pushState(null, "", nextRoute);
    setRoute(nextRoute);
  }

  async function refreshContacts(nextSelectedId = selectedId) {
    const data = await getContacts();
    setOverview(data.summary);
    setContacts(data.contacts);
    const contactId = nextSelectedId || data.contacts.find((item) => item.message_count > 0)?.contact_id || data.contacts[0]?.contact_id || "";
    setSelectedId(contactId);
    setDetail(contactId ? await getContact(contactId) : null);
  }

  async function refreshStatus() {
    try {
      setStatus(await getBumbleStatus());
    } catch {
      setStatus(null);
    }
  }

  async function refreshAndroidStatuses() {
    const next = { ...androidStatuses };
    await Promise.allSettled(
      ANDROID_APPS_CONFIG.map(async ({ key }) => {
        try {
          next[key] = await getAndroidStatus(key);
        } catch {
          next[key] = null;
        }
      })
    );
    setAndroidStatuses({ ...next });
  }

  async function handleStartAndroid() {
    setError("");
    const payload: AndroidRunPayload = {
      adb_address: androidAdbAddress,
      auto_send_enabled: androidAutoSend,
      poll_seconds: androidPollSeconds,
    };
    const result = await startAndroid(selectedAndroidApp, payload);
    setAndroidStatuses((prev) => ({ ...prev, [selectedAndroidApp]: result }));
  }

  async function handleStopAndroid() {
    setError("");
    const result = await stopAndroid(selectedAndroidApp);
    setAndroidStatuses((prev) => ({ ...prev, [selectedAndroidApp]: result }));
  }

  useEffect(() => {
    const onPopState = () => setRoute(currentRoute());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    refreshContacts().catch((err) => setError(err.message));
    refreshStatus();
    refreshAndroidStatuses();
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      refreshStatus();
      refreshAndroidStatuses();
      if (route === "/board") refreshContacts(selectedId).catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [route, selectedId]);

  const selectedContact = useMemo(
    () => contacts.find((item) => item.contact_id === selectedId),
    [contacts, selectedId]
  );

  async function selectContact(contactId: string) {
    setSelectedId(contactId);
    setDetail(await getContact(contactId));
    setDraftResult(null);
  }

  async function handleStartBumble() {
    setError("");
    setStatus(
      await startBumble({
        target_url: targetUrl,
        auto_send_enabled: autoSend,
        poll_seconds: pollSeconds,
        refresh_profile: refreshProfile
      })
    );
  }

  async function handleStopBumble() {
    setError("");
    setStatus(await stopBumble());
  }

  async function handleDraft() {
    if (!selectedId || !draftInput.trim()) return;
    setError("");
    const result = await createDraft({
      contact_id: selectedId,
      message: draftInput.trim(),
      channel: selectedContact?.channel || "manual",
      contact_profile: String(detail?.contact?.profile || ""),
      contact_identity: String(detail?.contact?.identity || ""),
      relationship_stage: String(detail?.contact?.relationship_stage || ""),
      recent_emotion: String(detail?.contact?.recent_emotion || ""),
      interaction_frequency: String(detail?.contact?.interaction_frequency || "")
    });
    setDraftResult(result);
    setDraftInput("");
    await refreshContacts(selectedId);
  }

  return (
    <main>
      <nav className="top-nav" aria-label="Primary navigation">
        <button className={route === "/about" ? "nav-active" : ""} onClick={() => navigate("/about")}>ABOUT</button>
        <button className={route === "/agent" ? "nav-active" : ""} onClick={() => navigate("/agent")}>Agent控制台</button>
        <button className={route === "/board" ? "nav-active" : ""} onClick={() => navigate("/board")}>联系人看板</button>
      </nav>

      {route === "/about" && <AboutPage />}
      {route === "/agent" && (
        <AgentPage
          status={status}
          targetUrl={targetUrl}
          autoSend={autoSend}
          refreshProfile={refreshProfile}
          pollSeconds={pollSeconds}
          setTargetUrl={setTargetUrl}
          setAutoSend={setAutoSend}
          setRefreshProfile={setRefreshProfile}
          setPollSeconds={setPollSeconds}
          handleStartBumble={() => handleStartBumble().catch((err) => setError(err.message))}
          handleStopBumble={() => handleStopBumble().catch((err) => setError(err.message))}
          androidStatuses={androidStatuses}
          selectedAndroidApp={selectedAndroidApp}
          androidAdbAddress={androidAdbAddress}
          androidAutoSend={androidAutoSend}
          androidPollSeconds={androidPollSeconds}
          setSelectedAndroidApp={setSelectedAndroidApp}
          setAndroidAdbAddress={setAndroidAdbAddress}
          setAndroidAutoSend={setAndroidAutoSend}
          setAndroidPollSeconds={setAndroidPollSeconds}
          handleStartAndroid={() => handleStartAndroid().catch((err) => setError(err.message))}
          handleStopAndroid={() => handleStopAndroid().catch((err) => setError(err.message))}
        />
      )}
      {route === "/board" && (
        <BoardPage
          overview={overview}
          contacts={contacts}
          selectedId={selectedId}
          selectedContact={selectedContact}
          detail={detail}
          draftInput={draftInput}
          draftResult={draftResult}
          setDraftInput={setDraftInput}
          selectContact={(id) => selectContact(id).catch((err) => setError(err.message))}
          refresh={() => refreshContacts(selectedId).catch((err) => setError(err.message))}
          handleDraft={() => handleDraft().catch((err) => setError(err.message))}
        />
      )}

      {error && <div className="error-toast">{error}</div>}
    </main>
  );
}

function AboutPage() {
  return (
    <section className="page about-page">
      <div className="hero-layout">
        <div className="hero-copy">
          <p className="kicker">LOCAL SOCIAL STRATEGY ENGINE</p>
          <h1>社交数字分身</h1>
          <p className="hero-subtitle">
            本地 Bumble 社交控制台：画像、记忆、RAG 案例、回复策略、自动化 Agent。
          </p>
        </div>
        <div className="character-stage">
          <img className="hero-character" src={heroCharacter} alt="社交数字分身角色" />
        </div>
      </div>

      <div className="about-band">
        <div className="section-label">ABOUT / USE</div>
        <div className="about-grid">
          <article>
            <h2>产品是什么</h2>
            <p>本地社交策略数字分身。它读取资料和对话，沉淀联系人画像，检索相似案例，生成短句回复。</p>
          </article>
          <article>
            <h2>使用方法</h2>
            <p>进入浏览器 Agent，启动 Bumble。登录态保存在本地，消息、画像和回复策略进入联系人看板。</p>
          </article>
        </div>
      </div>
    </section>
  );
}

function StatusPanel({ status }: { status: BumbleStatus | AndroidStatus | null }) {
  return (
    <div className="status-panel">
      <div className="status-head">
        <span className={status?.running ? "live-dot on" : "live-dot"} />
        <strong>{status?.stage || "OFFLINE"}</strong>
        <span>#{status?.status_code ?? "000"}</span>
      </div>
      <div className="metrics">
        <div><b>{status?.contact_count ?? 0}</b><span>待回复联系人</span></div>
        <div><b>{status?.draft_count ?? 0}</b><span>草稿</span></div>
        <div><b>{status?.sent_count ?? 0}</b><span>发送</span></div>
        <div><b>{status?.pending_group_count ?? 0}</b><span>待处理消息</span></div>
      </div>
      <p className="last-line">
        {(status as BumbleStatus | null)?.last_error ||
          (status as AndroidStatus | null)?.last_error ||
          (status as BumbleStatus | null)?.last_draft ||
          (status as AndroidStatus | null)?.last_draft ||
          "等待 Agent 状态。"}
      </p>
      <div className="log-list">
        {(status?.logs || []).slice(-8).reverse().map((log) => (
          <div className="log-line" key={`${log.time}-${log.stage}-${log.message}`}>
            <span>{log.time}</span>
            <b>{log.stage}</b>
            <em>{log.ok ? "OK" : "ERR"}</em>
            <p>{log.message}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function AgentPage(props: {
  status: BumbleStatus | null;
  targetUrl: string;
  autoSend: boolean;
  refreshProfile: boolean;
  pollSeconds: number;
  setTargetUrl: (value: string) => void;
  setAutoSend: (value: boolean) => void;
  setRefreshProfile: (value: boolean) => void;
  setPollSeconds: (value: number) => void;
  handleStartBumble: () => void;
  handleStopBumble: () => void;
  androidStatuses: Record<AndroidAppKey, AndroidStatus | null>;
  selectedAndroidApp: AndroidAppKey;
  androidAdbAddress: string;
  androidAutoSend: boolean;
  androidPollSeconds: number;
  setSelectedAndroidApp: (value: AndroidAppKey) => void;
  setAndroidAdbAddress: (value: string) => void;
  setAndroidAutoSend: (value: boolean) => void;
  setAndroidPollSeconds: (value: number) => void;
  handleStartAndroid: () => void;
  handleStopAndroid: () => void;
}) {
  const androidStatus = props.androidStatuses[props.selectedAndroidApp];
  return (
    <section className="page agent-page">
      <div className="section-label">BROWSER AGENT / BUMBLE</div>
      <div className="agent-layout">
        <div className="agent-panel">
          <h2>进入 Bumble</h2>
          <label>
            Bumble URL
            <input value={props.targetUrl} onChange={(event) => props.setTargetUrl(event.target.value)} />
          </label>
          <div className="control-row">
            <label className="checkbox-label">
              <input type="checkbox" checked={props.autoSend} onChange={(event) => props.setAutoSend(event.target.checked)} />
              自动发送
            </label>
            <label className="checkbox-label">
              <input type="checkbox" checked={props.refreshProfile} onChange={(event) => props.setRefreshProfile(event.target.checked)} />
              强制刷新画像
            </label>
          </div>
          <label>
            轮询秒数
            <input type="number" min={2} value={props.pollSeconds} onChange={(event) => props.setPollSeconds(Number(event.target.value))} />
          </label>
          <div className="button-row">
            <button onClick={props.handleStartBumble}>START</button>
            <button className="ghost" onClick={props.handleStopBumble}>STOP</button>
          </div>
        </div>
        <StatusPanel status={props.status} />
      </div>

      <div className="section-label" style={{ marginTop: "2rem" }}>ANDROID AGENT / 手机 APP</div>
      <div className="agent-layout">
        <div className="agent-panel">
          <h2>Android 模拟器自动回复</h2>
          <div className="control-row" style={{ flexWrap: "wrap", gap: "0.4rem" }}>
            {ANDROID_APPS_CONFIG.map(({ key, label }) => (
              <button
                key={key}
                className={props.selectedAndroidApp === key ? "" : "ghost"}
                style={{ padding: "0.3rem 0.8rem", fontSize: "0.85rem" }}
                onClick={() => props.setSelectedAndroidApp(key)}
              >
                {label}
                {props.androidStatuses[key]?.running && <span style={{ marginLeft: "0.3rem", color: "#4caf50" }}>●</span>}
              </button>
            ))}
          </div>
          <label>
            ADB 地址（模拟器端口）
            <input
              value={props.androidAdbAddress}
              onChange={(event) => props.setAndroidAdbAddress(event.target.value)}
              placeholder="127.0.0.1:7555"
            />
          </label>
          <div className="control-row">
            <label className="checkbox-label">
              <input type="checkbox" checked={props.androidAutoSend} onChange={(event) => props.setAndroidAutoSend(event.target.checked)} />
              自动发送
            </label>
          </div>
          <label>
            轮询秒数
            <input type="number" min={3} value={props.androidPollSeconds} onChange={(event) => props.setAndroidPollSeconds(Number(event.target.value))} />
          </label>
          <div className="button-row">
            <button onClick={props.handleStartAndroid}>START</button>
            <button className="ghost" onClick={props.handleStopAndroid}>STOP</button>
          </div>
        </div>
        <StatusPanel status={androidStatus} />
      </div>
    </section>
  );
}

function BoardPage(props: {
  overview: Overview;
  contacts: ContactSummary[];
  selectedId: string;
  selectedContact?: ContactSummary;
  detail: ContactDetail | null;
  draftInput: string;
  draftResult: DraftResult | null;
  setDraftInput: (value: string) => void;
  selectContact: (contactId: string) => void;
  refresh: () => void;
  handleDraft: () => void;
}) {
  const [searchQuery, setSearchQuery] = useState("");
  const filteredContacts = searchQuery.trim()
    ? props.contacts.filter((c) =>
        (c.display_name || c.contact_id || "").toLowerCase().includes(searchQuery.toLowerCase())
      )
    : props.contacts;

  return (
    <section className="page board-page">
      <div className="section-label">CONTACT BOARD / MEMORY</div>
      <div className="summary-banner">
        <div><b>{props.overview.contact_count}</b><span>联系人</span></div>
        <div><b>{props.overview.message_count}</b><span>消息</span></div>
        <div><b>{props.overview.reply_count}</b><span>AI回复</span></div>
        <div><b>{props.overview.sent_count}</b><span>已发送</span></div>
      </div>

      <div className="contact-layout">
        <aside className="contact-list">
          <input
            className="contact-search"
            type="text"
            placeholder="搜索联系人..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
          {filteredContacts.length === 0 && <p className="empty">{searchQuery ? "无匹配联系人" : "暂无联系人。若已有历史记录，请重启 FastAPI 后端。"}</p>}
          {filteredContacts.map((contact) => (
            <button
              className={contact.contact_id === props.selectedId ? "contact-card active" : "contact-card"}
              key={contact.contact_id}
              onClick={() => props.selectContact(contact.contact_id)}
            >
              <span className="contact-time">{formatTime(contact.last_message_at || contact.updated_at)}</span>
              <strong>{contactName(contact)}</strong>
              <small>{contact.channel || "bumble"} · {shortContactId(contact.contact_id)}</small>
              <p>{readableProfile(contact.profile, contact.last_message)}</p>
              <em>{contact.channel || "bumble"} · {contact.message_count} messages</em>
            </button>
          ))}
        </aside>

        <div className="detail-panel">
          <header className="detail-head">
            <div>
              <span>SELECTED CONTACT</span>
              <h2>{contactName(props.selectedContact)}</h2>
              <p>{props.selectedId ? `${props.selectedContact?.channel || "bumble"} · ${shortContactId(props.selectedId)}` : "NO CONTACT"}</p>
            </div>
            <button className="ghost" onClick={props.refresh}>REFRESH</button>
          </header>

          <div className="profile-grid">
            {visibleProfileFields(props.detail).map((field) => (
              <div className="profile-pill" key={field.field}>
                <span>{fieldLabels[field.field] || field.field}</span>
                <b>{readableFieldValue(field.field, field.value)}</b>
              </div>
            ))}
            {props.detail && Object.keys(props.detail.fields || {}).length === 0 && (
              <p className="empty">暂无画像字段。Bumble Agent 读取 profile 后会自动补全。</p>
            )}
          </div>

          <div className="draft-box">
            <textarea
              value={props.draftInput}
              onChange={(event) => props.setDraftInput(event.target.value)}
              placeholder="在当前联系人下追加一条本地消息，并生成回复草稿"
            />
            <button onClick={props.handleDraft}>生成并写入记录</button>
            {props.draftResult && (
              <div className="draft-result">
                <b>{props.draftResult.draft}</b>
                <span>{props.draftResult.technique} / {props.draftResult.scenario}</span>
                <p>{props.draftResult.decision_reason}</p>
              </div>
            )}
          </div>

          <div className="message-timeline">
            {(props.detail?.messages || []).map((message) => (
              <article className={`message-row ${message.role}`} key={message.id}>
                <div>
                  <span>{message.role}</span>
                  <time>{formatTime(message.created_at)}</time>
                </div>
                <p>{message.content}</p>
                {(message.technique || message.decision_reason) && (
                  <footer>
                    <b>{message.technique || "strategy"}</b>
                    <span>{message.decision_reason}</span>
                  </footer>
                )}
              </article>
            ))}
            {props.detail && props.detail.messages.length === 0 && <p className="empty">暂无消息记录。</p>}
          </div>
        </div>
      </div>
    </section>
  );
}
