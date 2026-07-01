import { useEffect, useMemo, useRef, useState } from "react";
import heroCharacter from "./assets/hero-character.png";
import {
  AndroidRunPayload,
  AndroidStatus,
  BumbleStatus,
  ContactDetail,
  ProfileField,
  ContactSummary,
  DraftResult,
  createDraft,
  getAndroidStatus,
  getBumbleStatus,
  getContact,
  getContactDebug,
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

function contactPlatform(c: ContactSummary): "bumble" | "tantan" | "other" {
  const ch = (c.channel || "").toLowerCase();
  if (ch === "bumble" || c.contact_id.startsWith("bumble:")) return "bumble";
  if (ch === "tantan" || ch === "qianshou") return "tantan";
  return "other";
}

function platformLabel(key: "bumble" | "tantan" | "other") {
  if (key === "bumble") return "Bumble";
  if (key === "tantan") return "探探";
  return "其他";
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
  about_me: "关于我",
  age: "年龄",
  company: "公司",
  compatibility_points: "契合点",
  bio: "简介",
  dating_intentions: "关系意图",
  drinking: "饮酒",
  education: "教育",
  exercise: "运动",
  family_plans: "家庭计划",
  gender: "性别",
  height: "身高",
  hometown: "家乡",
  interests_hobbies: "兴趣爱好",
  job: "工作",
  location: "位置",
  name: "名字",
  personality_traits: "性格特征",
  platform_name: "平台名",
  profile_prompts: "主页问答",
  raw_evidence: "原始证据",
  school: "学校",
  zodiac: "星座",
  hobbies: "兴趣"
};

const fieldAliases: Record<string, string> = {
  bio: "about_me",
  hobbies: "interests_hobbies",
  interest_tags: "interests_hobbies",
  personality: "personality_traits",
  platform_name: "name"
};

const profileSections: Array<{ title: string; fields: string[] }> = [
  { title: "基本信息", fields: ["name", "age", "height", "education", "job", "company", "school", "zodiac", "location", "hometown"] },
  { title: "关于我", fields: ["about_me"] },
  { title: "性格特征", fields: ["personality_traits"] },
  { title: "兴趣爱好", fields: ["interests_hobbies"] },
  { title: "主页问答", fields: ["profile_prompts"] },
  { title: "契合点", fields: ["compatibility_points"] },
  { title: "原始证据", fields: ["raw_evidence"] }
];

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

function readableJson(value: unknown) {
  if (!value) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function normalizedProfileFields(detail: ContactDetail | null) {
  const result: Record<string, ProfileField> = {};
  Object.values(detail?.fields || {}).forEach((field) => {
    if (!field.value || field.value.includes("euri=")) return;
    const normalized = fieldAliases[field.field] || field.field;
    if (normalized === "photo_urls" || normalized === "photo_description") return;
    const current = result[normalized];
    const next = { ...field, field: normalized };
    if (!current || next.confidence >= current.confidence) {
      result[normalized] = next;
    }
  });
  return result;
}

function contactDetailThreadId(detail: ContactDetail | null) {
  return detail?.thread?.thread_id || detail?.conversations?.[0]?.conversation_id || "";
}

export default function App() {
  const [route, setRoute] = useState<Route>(currentRoute());
  const [overview, setOverview] = useState<Overview>(defaultOverview);
  const [contacts, setContacts] = useState<ContactSummary[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<ContactDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const detailRequestId = useRef(0);
  const [status, setStatus] = useState<BumbleStatus | null>(null);
  const [targetUrl, setTargetUrl] = useState("https://eu1.bumble.com/app/connections");
  const [autoSend, setAutoSend] = useState(true);
  const [refreshProfile, setRefreshProfile] = useState(false);
  const [pollSeconds, setPollSeconds] = useState(5);
  const [draftInput, setDraftInput] = useState("");
  const [draftResult, setDraftResult] = useState<DraftResult | null>(null);
  const [error, setError] = useState("");
  const [selectedAndroidApp, setSelectedAndroidApp] = useState<AndroidAppKey>("tantan");
  const [androidAdbAddress, setAndroidAdbAddress] = useState("");
  const [androidAutoSend, setAndroidAutoSend] = useState(true);
  const [androidPollSeconds, setAndroidPollSeconds] = useState(8);
  const [androidStatuses, setAndroidStatuses] = useState<Record<AndroidAppKey, AndroidStatus | null>>(
    Object.fromEntries(ANDROID_APPS_CONFIG.map((a) => [a.key, null])) as Record<AndroidAppKey, AndroidStatus | null>
  );

  function navigate(nextRoute: Route) {
    window.history.pushState(null, "", nextRoute);
    setRoute(nextRoute);
  }

  async function loadContactDetail(contactId: string, clearCurrent = true) {
    const requestId = detailRequestId.current + 1;
    detailRequestId.current = requestId;
    if (clearCurrent) setDetail(null);
    if (!contactId) {
      setDetail(null);
      setDetailLoading(false);
      return;
    }
    setDetailLoading(true);
    try {
      const nextDetail = await getContact(contactId);
      if (detailRequestId.current === requestId) setDetail(nextDetail);
    } finally {
      if (detailRequestId.current === requestId) setDetailLoading(false);
    }
  }

  async function refreshContacts(nextSelectedId = selectedId) {
    const data = await getContacts();
    setOverview(data.summary);
    setContacts(data.contacts);
    const contactId = nextSelectedId || data.contacts.find((item) => item.message_count > 0)?.contact_id || data.contacts[0]?.contact_id || "";
    setSelectedId(contactId);
    await loadContactDetail(contactId, contactId !== selectedId);
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
  const selectedDetail = contactDetailThreadId(detail) === selectedId ? detail : null;

  async function selectContact(contactId: string) {
    setSelectedId(contactId);
    await loadContactDetail(contactId);
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
          detail={selectedDetail}
          detailLoading={detailLoading}
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

type ModalMode = "messages" | "profile" | "llm" | null;

const LLM_PROMPT_BLOCKS: Array<{ key: string; label: string; bg: string; color: string }> = [
  { key: "profile_text_analysis", label: "① Profile 文本解析", bg: "var(--pink)", color: "var(--ink)" },
  { key: "profile_image_analysis", label: "② Profile 图片解析", bg: "var(--pink)", color: "var(--ink)" },
  { key: "memory_update", label: "③ 长期记忆更新", bg: "var(--purple)", color: "var(--paper)" },
  { key: "technique_decision", label: "④ 技术选择", bg: "var(--purple)", color: "var(--paper)" },
  { key: "reply_generation", label: "⑤ 回复生成", bg: "var(--acid)", color: "var(--ink)" },
  { key: "reply_rewrite", label: "⑥ 回复改写（质检失败时触发）", bg: "var(--acid)", color: "var(--ink)" },
];

function checkProfileBeforeMemoryOrder(prompts: Record<string, { prompt: string; model: string; at: string }>): string | null {
  const pa = prompts["profile_text_analysis"];
  const mu = prompts["memory_update"];
  if (!pa || !mu) return null;
  if (pa.at > mu.at) {
    return `排序违规：profile 解析（${pa.at.slice(0, 16)}）晚于记忆更新（${mu.at.slice(0, 16)}）`;
  }
  return null;
}

function LlmPromptsModal({ contactName, prompts, onClose }: {
  contactName: string;
  prompts: Record<string, { prompt: string; model: string; at: string }>;
  onClose: () => void;
}) {
  const hasAny = Object.keys(prompts).length > 0;
  const orderError = checkProfileBeforeMemoryOrder(prompts);
  return (
    <Modal title={`LLM 输入 · ${contactName}`} onClose={onClose}>
      {orderError && (
        <div style={{ background: "#ff2d2d", color: "#fff", padding: "10px 14px", marginBottom: 16, fontWeight: 700, fontSize: 13, borderRadius: 4 }}>
          ⚠ {orderError}
        </div>
      )}
      {!hasAny && (
        <p style={{ color: "var(--paper)" }}>暂无记录。Agent 为该联系人触发相应动作后自动写入。</p>
      )}
      {LLM_PROMPT_BLOCKS.map(({ key, label, bg, color }) => {
        const entry = prompts[key];
        if (!entry) return null;
        return (
          <div key={key} style={{ marginBottom: 24 }}>
            <div style={{ padding: "8px 12px", background: bg, color, fontSize: 12, fontWeight: 950, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 8, display: "flex", justifyContent: "space-between" }}>
              <span>{label}</span>
              <span style={{ opacity: 0.7 }}>{entry.model} · {entry.at?.slice(0, 16)}</span>
            </div>
            <pre>{entry.prompt}</pre>
          </div>
        );
      })}
    </Modal>
  );
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) { if (e.key === "Escape") onClose(); }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-box" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button onClick={onClose}>✕ 关闭</button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

function BoardPage(props: {
  overview: Overview;
  contacts: ContactSummary[];
  selectedId: string;
  selectedContact?: ContactSummary;
  detail: ContactDetail | null;
  detailLoading: boolean;
  draftInput: string;
  draftResult: DraftResult | null;
  setDraftInput: (value: string) => void;
  selectContact: (contactId: string) => void;
  refresh: () => void;
  handleDraft: () => void;
}) {
  const [searchQuery, setSearchQuery] = useState("");
  const [modal, setModal] = useState<ModalMode>(null);
  const [platformFilter, setPlatformFilter] = useState<"all" | "bumble" | "tantan" | "other">("all");
  const [detailTab, setDetailTab] = useState<"recent" | "profile" | "memory" | "operations">("recent");

  useEffect(() => {
    setModal(null);
    setDetailTab("recent");
  }, [props.selectedId]);

  const platformsPresent = useMemo(() => {
    const set = new Set<string>();
    props.contacts.forEach((c) => set.add(contactPlatform(c)));
    return set;
  }, [props.contacts]);

  const filteredContacts = useMemo(() => {
    let list = props.contacts;
    if (platformFilter !== "all") list = list.filter((c) => contactPlatform(c) === platformFilter);
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      list = list.filter((c) => (c.display_name || c.contact_id || "").toLowerCase().includes(q));
    }
    return list;
  }, [props.contacts, platformFilter, searchQuery]);

  const normalizedFields = normalizedProfileFields(props.detail);

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
        {/* ── LEFT: contact list ── */}
        <aside className="contact-list">
          <div className="platform-tabs">
            {(["all", "bumble", "tantan", "other"] as const)
              .filter((key) => key === "all" || platformsPresent.has(key))
              .map((key) => (
                <button
                  key={key}
                  className={platformFilter === key ? "active" : ""}
                  onClick={() => setPlatformFilter(key)}
                >
                  {key === "all" ? "全部" : platformLabel(key)}
                </button>
              ))}
          </div>
          <input
            className="contact-search"
            type="text"
            placeholder="搜索联系人..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
          {filteredContacts.length === 0 && (
            <p className="empty">{searchQuery ? "无匹配联系人" : "暂无联系人。若已有历史记录，请重启 FastAPI 后端。"}</p>
          )}
          {filteredContacts.map((contact) => {
            const plat = contactPlatform(contact);
            return (
              <button
                className={contact.contact_id === props.selectedId ? "contact-card active" : "contact-card"}
                key={contact.contact_id}
                onClick={() => props.selectContact(contact.contact_id)}
              >
                <div className="contact-card-header">
                  <strong>{contactName(contact)}</strong>
                  <span className="contact-time">{formatTime(contact.last_message_at || contact.updated_at)}</span>
                </div>
                <div className="contact-card-meta">
                  <span className={`platform-badge platform-badge--${plat}`}>{platformLabel(plat)}</span>
                  <small>{shortContactId(contact.contact_id)}</small>
                  <small>· {contact.message_count} 条</small>
                </div>
                <p>{readableProfile(contact.profile, contact.last_message)}</p>
              </button>
            );
          })}
        </aside>

        {/* ── RIGHT: detail panel ── */}
        <div className="detail-panel">
          <header className="detail-head">
            <div>
              <span className="detail-kicker">SELECTED CONTACT</span>
              <h2 className="detail-name">{contactName(props.selectedContact)}</h2>
              <div className="detail-meta">
                {props.selectedContact && (
                  <span className={`platform-badge platform-badge--${contactPlatform(props.selectedContact)}`}>
                    {platformLabel(contactPlatform(props.selectedContact))}
                  </span>
                )}
                <span className="detail-id">{props.selectedId ? shortContactId(props.selectedId) : "NO CONTACT"}</span>
              </div>
            </div>
            <button className="ghost" onClick={props.refresh}>REFRESH</button>
          </header>

          {/* ── Tab nav ── */}
          <div className="detail-tabs">
            {(["recent", "profile", "memory", "operations"] as const).map((tab) => (
              <button
                key={tab}
                className={detailTab === tab ? "active" : ""}
                onClick={() => setDetailTab(tab)}
              >
                {tab === "recent"
                  ? `近期${props.detail ? ` (${props.detail.messages.length})` : ""}`
                  : tab === "profile" ? "画像"
                  : tab === "memory" ? "记忆"
                  : "操作"}
              </button>
            ))}
          </div>

          {/* ── Tab: 近期 ── */}
          {detailTab === "recent" && (
            <div className="tab-content">
              {props.detail?.pending_group && props.detail.pending_group.length > 0 && (
                <div className="pending-group-box">
                  <div className="pending-group-label">待处理消息组 · {props.detail.pending_group.length} 条未回复</div>
                  {props.detail.pending_group.map((msg, i) => (
                    <div key={i} className="pending-group-item">
                      <span>{String(msg.role || "user")}</span>
                      <p>{String(msg.content || "")}</p>
                    </div>
                  ))}
                </div>
              )}
              <div className="message-timeline">
                {props.detailLoading && <p className="empty">加载中...</p>}
                {!props.detailLoading && !props.detail && (
                  <p className="empty">请从左侧选择联系人。</p>
                )}
                {(props.detail?.messages || []).slice(-10).map((message) => (
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
                {props.detail && props.detail.messages.length === 0 && (
                  <p className="empty">暂无消息记录。</p>
                )}
              </div>
              {props.detail && props.detail.messages.length > 0 && (
                <button className="view-all-btn" onClick={() => setModal("messages")}>
                  查看全部 {props.detail.messages.length} 条历史消息 →
                </button>
              )}
            </div>
          )}

          {/* ── Tab: 画像 ── */}
          {detailTab === "profile" && (
            <div className="tab-content">
              <div className="profile-grid">
                {profileSections.map((section) => {
                  const sectionFields = section.fields.map((field) => normalizedFields[field]).filter(Boolean);
                  return (
                    <section className="profile-section" key={section.title}>
                      <h3>{section.title}</h3>
                      {sectionFields.length > 0 ? (
                        <div className="profile-section-fields">
                          {sectionFields.map((field) => {
                            const label = fieldLabels[field.field] || field.field;
                            const showLabel = section.fields.length > 1 || label !== section.title;
                            return (
                              <div className="profile-pill" key={field.field}>
                                {showLabel && <span>{label}</span>}
                                <b>{readableFieldValue(field.field, field.value)}</b>
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <p className="empty">{props.detailLoading ? "加载中" : "暂无"}</p>
                      )}
                    </section>
                  );
                })}
                {props.detail && Object.keys(normalizedFields).length === 0 && (
                  <p style={{ color: "var(--paper)", gridColumn: "1 / -1" }}>
                    暂无画像字段。Bumble Agent 读取 profile 后会自动补全。
                  </p>
                )}
              </div>
              <button className="view-all-btn" onClick={() => setModal("profile")}>
                查看原始 Profile 文本 →
              </button>
            </div>
          )}

          {/* ── Tab: 记忆 ── */}
          {detailTab === "memory" && (
            <div className="tab-content">
              {!props.detail && <p className="empty">请从左侧选择联系人。</p>}
              <div className="memory-grid">
                <div className="memory-section">
                  <h4>长期摘要 / working summary</h4>
                  <p>{readableJson(props.detail?.memory?.working_summary) || "暂无"}</p>
                </div>
                <div className="memory-section">
                  <h4>固定事实 / pinned facts</h4>
                  <pre>{readableJson(props.detail?.memory?.pinned_facts) || "暂无"}</pre>
                </div>
                <div className="memory-section">
                  <h4>偏好与禁忌 / preferences · taboos</h4>
                  <pre>{readableJson({ preferences: props.detail?.memory?.preferences, taboos: props.detail?.memory?.taboos })}</pre>
                </div>
                <div className="memory-section">
                  <h4>话题历史 / topic history</h4>
                  <pre>{readableJson(props.detail?.memory?.topic_history) || "暂无"}</pre>
                </div>
                <div className="memory-section">
                  <h4>回复历史 / reply history</h4>
                  <pre>{readableJson(props.detail?.memory?.reply_history) || "暂无"}</pre>
                </div>
                <div className="memory-section">
                  <h4>待处理消息组 / pending group</h4>
                  <pre>{readableJson(props.detail?.pending_group) || "暂无"}</pre>
                </div>
              </div>
            </div>
          )}

          {/* ── Tab: 操作 ── */}
          {detailTab === "operations" && (
            <div className="tab-content">
              <div className="draft-box">
                <div className="draft-box-label">追加消息并生成草稿</div>
                <textarea
                  value={props.draftInput}
                  onChange={(event) => props.setDraftInput(event.target.value)}
                  placeholder="输入联系人的最新消息，系统会生成 AI 回复草稿"
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
              {(() => {
                const orderErr = checkProfileBeforeMemoryOrder(props.detail?.last_llm_prompts ?? {});
                return orderErr ? (
                  <div style={{ background: "#ff2d2d", color: "#fff", padding: "8px 12px", marginTop: 12, fontWeight: 700, fontSize: 12, borderRadius: 4 }}>
                    ⚠ {orderErr}
                  </div>
                ) : null;
              })()}
              <button className="view-all-btn" style={{ marginTop: 12 }} onClick={() => setModal("llm")}>
                LLM 输入记录（{Object.keys(props.detail?.last_llm_prompts ?? {}).length} 个区块）→
              </button>
            </div>
          )}
        </div>
      </div>

      {/* ── Modals ── */}
      {modal === "messages" && (
        <Modal title={`历史消息 · ${contactName(props.selectedContact)} · 共 ${props.detail?.messages?.length ?? 0} 条`} onClose={() => setModal(null)}>
          {(props.detail?.messages || []).map((message) => (
            <article className={`message-row ${message.role}`} key={message.id} style={{ marginBottom: "12px" }}>
              <div>
                <span>{message.role}</span>
                <time>{formatTime(message.created_at)}</time>
              </div>
              <code className="trace-id">trace {shortContactId(message.message_id || String(message.id))}</code>
              <p>{message.content}</p>
              {(message.technique || message.decision_reason) && (
                <footer>
                  <b>{message.technique || "strategy"}</b>
                  <span>{message.decision_reason}</span>
                </footer>
              )}
            </article>
          ))}
          {(!props.detail || props.detail.messages.length === 0) && (
            <p style={{ color: "var(--paper)" }}>暂无消息记录。</p>
          )}
        </Modal>
      )}

      {modal === "profile" && (
        <Modal title={`原始 Profile · ${contactName(props.selectedContact)}`} onClose={() => setModal(null)}>
          {props.detail?.profile_text
            ? <pre>{props.detail.profile_text}</pre>
            : <p style={{ color: "var(--paper)" }}>尚未采集到原始 Profile 文本。Bumble Agent 打开联系人后会自动写入。</p>
          }
        </Modal>
      )}

      {modal === "llm" && (
        <LlmPromptsModal
          contactName={contactName(props.selectedContact)}
          prompts={props.detail?.last_llm_prompts ?? {}}
          onClose={() => setModal(null)}
        />
      )}
    </section>
  );
}
