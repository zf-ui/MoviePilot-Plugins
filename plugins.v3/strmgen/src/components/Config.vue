<template>
  <v-form class="sm-config">
    <!-- 顶部：名称、一句话说明、总开关 -->
    <v-sheet rounded="xl" class="hero pa-5 pl-6 mb-4" :class="{ 'hero--off': !form.enabled }">
      <div class="hero__glow" />
      <div class="d-flex align-center ga-4 flex-wrap hero__inner">
        <v-avatar size="52" rounded="xl" class="hero__icon">
          <v-icon icon="mdi-cloud-sync-outline" size="28" />
        </v-avatar>
        <div class="flex-grow-1" style="min-width: 200px">
          <div class="d-flex align-center flex-wrap ga-2">
            <span class="text-h6 font-weight-bold">Strm Media</span>
            <span class="hero__pill">
              <span class="hero__dot" :class="{ 'hero__dot--on': form.enabled }" />
              {{ form.enabled ? "运行中" : "已暂停" }}
            </span>
          </div>
          <div class="text-body-2 hero__sub mt-1">115 目录 → 本地 STRM → 播放时经 MediaWarp 302 直链</div>
        </div>
        <label class="hero__switch d-flex align-center ga-3 px-4 py-2">
          <span class="text-body-2 font-weight-medium">启用插件</span>
          <v-switch v-model="form.enabled" color="success" hide-details density="compact" inset />
        </label>
      </div>
    </v-sheet>

    <v-row>
      <!-- 左侧分区导航（窄屏时变为顶部横向页签） -->
      <v-col cols="12" md="3" lg="2">
        <nav class="nav-wrap">
          <div class="nav">
            <div class="nav__caption px-3 pt-3 pb-1">设置步骤</div>
            <div
              v-for="item in navItems"
              :key="item.id"
              class="nav__item"
              :class="{ 'nav__item--active': active === item.id, 'nav__item--sub': item.sub }"
              @click="scrollTo(item.id)"
            >
              <span v-if="item.step" class="nav__num" :class="`nav__num--${item.state}`">
                <v-icon v-if="item.state === 'ok'" icon="mdi-check" size="14" />
                <template v-else>{{ item.step }}</template>
              </span>
              <span v-else class="nav__tick" />
              <v-icon :icon="item.icon" size="18" />
              <span class="nav__label">{{ item.label }}</span>
              <span v-if="item.state === 'warn'" class="nav__flag" title="这一步还有没填的内容" />
            </div>
          </div>
        </nav>
      </v-col>

      <!-- 右侧分步设置 -->
      <v-col cols="12" md="9" lg="10">
        <!-- 1. 115 连接 -->
        <SectionCard id="sec-cloud" icon="mdi-account-key-outline" title="115 网盘连接" subtitle="列目录、取直链都靠这份凭证" class="mb-4">
          <template #action>
            <v-chip size="small" label variant="tonal" :color="cookieMasked ? 'success' : 'warning'">
              <v-icon start :icon="cookieMasked ? 'mdi-check' : 'mdi-alert-outline'" size="14" />
              {{ cookieMasked ? "已保存" : "未配置" }}
            </v-chip>
          </template>
          <v-textarea
            v-model="form.cookies"
            variant="outlined"
            density="comfortable"
            rounded="lg"
            class="mono"
            :label="cookieMasked ? '115 Cookie（已保存，输入新值可覆盖）' : '115 Cookie'"
            placeholder="粘贴浏览器中的 115 Cookie"
            prepend-inner-icon="mdi-cookie-outline"
            rows="3"
            auto-grow
          />
        </SectionCard>

        <!-- 2. 扫描与输出 -->
        <SectionCard id="sec-scan" icon="mdi-folder-search-outline" title="扫描与输出" subtitle="扫哪些云端目录、STRM 落到哪里" class="mb-4">
          <template #action>
            <v-chip v-if="sourceCount" size="small" label variant="tonal" color="primary">{{ sourceCount }} 个目录</v-chip>
          </template>
          <v-textarea
            v-model="form.source_paths"
            variant="outlined"
            density="comfortable"
            rounded="lg"
            class="mono mb-4"
            label="监控的 115 目录"
            placeholder="/影视库/电视剧"
            prepend-inner-icon="mdi-cloud-outline"
            rows="2"
            auto-grow
            hint="每行一个或逗号分隔，以 / 开头"
            persistent-hint
          />
          <v-row dense>
            <v-col cols="12" md="7">
              <v-text-field
                v-model="form.dest_strm"
                variant="outlined"
                density="comfortable"
                rounded="lg"
                class="mono"
                label="本地 STRM 输出目录"
                placeholder="/data/strm"
                prepend-inner-icon="mdi-file-tree-outline"
              />
            </v-col>
            <v-col cols="12" md="5">
              <v-text-field
                v-model="form.video_extensions"
                variant="outlined"
                density="comfortable"
                rounded="lg"
                class="mono"
                label="视频扩展名"
                placeholder="mp4,mkv,ts"
                prepend-inner-icon="mdi-file-video-outline"
                hint="逗号分隔"
                persistent-hint
              />
            </v-col>
          </v-row>
        </SectionCard>

        <!-- 3. 链接与计划 -->
        <SectionCard id="sec-plan" icon="mdi-cog-outline" title="链接与计划" subtitle="STRM 里的链接前缀、何时扫描" class="mb-4">
          <v-row dense>
            <v-col cols="12" md="6">
              <v-text-field
                v-model="form.moviepilot_address"
                variant="outlined"
                density="comfortable"
                rounded="lg"
                class="mono"
                label="MoviePilot 外部访问地址"
                placeholder="http://192.168.1.10:3000"
                prepend-inner-icon="mdi-link-variant"
                hint="STRM 链接前缀，播放器需能访问；留空使用系统 APP_DOMAIN"
                persistent-hint
              />
            </v-col>
            <v-col cols="12" sm="6" md="3">
              <v-text-field
                v-model="form.cron"
                variant="outlined"
                density="comfortable"
                rounded="lg"
                class="mono"
                label="定时扫描 Cron"
                placeholder="0 4 * * *"
                prepend-inner-icon="mdi-clock-outline"
                :hint="cronHint"
                persistent-hint
              />
            </v-col>
            <v-col cols="12" sm="6" md="3">
              <SwitchRow
                v-model="form.delete_strm"
                title="同步删除 STRM"
                desc="云端删了本地也删"
                color="error"
                boxed
              />
            </v-col>
          </v-row>
        </SectionCard>

        <!-- 4. MediaWarp -->
        <SectionCard id="sec-warp" icon="mdi-shuffle-variant" title="MediaWarp 中间件" subtitle="挂在 Emby / Jellyfin 前面，播放 STRM 时改成直链" class="mb-0">
          <template #action>
            <v-chip size="small" label variant="tonal" :color="form.warp_enabled ? 'info' : 'default'">
              {{ form.warp_enabled ? `监听 :${form.warp_port}` : "未启用" }}
            </v-chip>
          </template>

          <SwitchRow
            v-model="form.warp_enabled"
            title="启用 MediaWarp 中间件"
            desc="随插件启停，自动下载 MediaWarp 二进制并生成配置"
            boxed
          />

          <v-expand-transition>
            <div v-if="form.warp_enabled">
              <v-row dense class="mt-3">
                <v-col cols="12" sm="4">
                  <v-text-field
                    v-model="form.warp_port"
                    variant="outlined"
                    density="comfortable"
                    rounded="lg"
                    class="mono"
                    label="监听端口"
                    placeholder="9096"
                    prepend-inner-icon="mdi-ethernet"
                  />
                </v-col>
                <v-col cols="12" sm="8">
                  <v-text-field
                    v-model="form.warp_mediaserver"
                    variant="outlined"
                    density="comfortable"
                    rounded="lg"
                    label="上游媒体服务器"
                    placeholder="emby"
                    prepend-inner-icon="mdi-server"
                    hint="与 MoviePilot「媒体服务器」中的名称一致；留空则不启动"
                    persistent-hint
                  />
                </v-col>
              </v-row>

              <!-- 直链重定向 -->
              <div id="sec-warp-redirect" class="subblock mt-4">
                <div class="subblock__head">
                  <v-icon icon="mdi-fast-forward-outline" size="16" color="primary" />
                  <span class="text-caption font-weight-bold tracking-wide">直链重定向</span>
                  <span class="text-caption text-medium-emphasis ml-2">按 STRM 内容格式选择</span>
                </div>
                <SwitchRow v-model="form.httpstrm_enabled" title="HTTPStrm 直链重定向" desc="STRM 内容为 HTTP URL 时生效" />
                <v-expand-transition>
                  <div v-if="form.httpstrm_enabled" class="indent pb-2">
                    <v-textarea
                      v-model="form.media_strm_path"
                      variant="outlined"
                      density="compact"
                      rounded="lg"
                      class="mono mb-3"
                      label="Strm 文件前缀"
                      placeholder="/data"
                      rows="1"
                      auto-grow
                      hint="每行一个；媒体服务器里符合前缀的 STRM 才会走 HTTPStrm"
                      persistent-hint
                    />
                    <SwitchRow
                      v-model="form.httpstrm_final_url"
                      title="FinalURL"
                      desc="先解析到最终直链再 302；公网播放时避免把局域网地址发给客户端"
                    />
                  </div>
                </v-expand-transition>

                <SwitchRow
                  v-model="form.aliststrm_enabled"
                  title="AlistStrm 直链重定向"
                  desc="STRM 内容为 Alist 文件路径时生效"
                  bordered
                />
                <v-expand-transition>
                  <div v-if="form.aliststrm_enabled" class="indent pb-2">
                    <v-textarea
                      v-model="form.alist_list"
                      variant="outlined"
                      density="compact"
                      rounded="lg"
                      class="mono code"
                      label="Alist 服务列表（YAML）"
                      rows="5"
                      auto-grow
                      hint="仅适配 Alist V3；每项含 ADDR 与 Username/Password 或 Token，及 PrefixList"
                      persistent-hint
                    />
                  </div>
                </v-expand-transition>
              </div>

              <!-- 播放增强 -->
              <div id="sec-warp-extra" class="subblock mt-4">
                <div class="subblock__head">
                  <v-icon icon="mdi-play-box-multiple-outline" size="16" color="primary" />
                  <span class="text-caption font-weight-bold tracking-wide">播放增强</span>
                  <span class="text-caption text-medium-emphasis ml-2">可选</span>
                </div>
                <SwitchRow v-model="form.video_together" title="共同观影" desc="VideoTogether，网页端同步播放进度" />
                <SwitchRow
                  v-model="form.client_filter_enabled"
                  title="客户端黑白名单"
                  desc="只对名单内 / 外的客户端生效"
                  color="warning"
                  bordered
                />
                <v-expand-transition>
                  <div v-if="form.client_filter_enabled" class="indent pb-2">
                    <v-row dense>
                      <v-col cols="12" sm="4">
                        <v-select
                          v-model="form.client_filter_mode"
                          variant="outlined"
                          density="compact"
                          rounded="lg"
                          label="过滤模式"
                          :items="filterModes"
                          hide-details
                        />
                      </v-col>
                      <v-col cols="12" sm="8">
                        <v-textarea
                          v-model="form.client_filter_list"
                          variant="outlined"
                          density="compact"
                          rounded="lg"
                          class="mono"
                          label="客户端名单"
                          placeholder="Fileball、Infuse"
                          rows="1"
                          auto-grow
                          hide-details
                        />
                      </v-col>
                    </v-row>
                  </div>
                </v-expand-transition>
              </div>
            </div>
          </v-expand-transition>
        </SectionCard>
      </v-col>
    </v-row>

    <!-- 底部固定操作栏 -->
    <div class="actions-wrap">
      <div class="actions d-flex align-center flex-wrap ga-3 px-4 py-3">
        <div class="flex-grow-1 d-flex align-center ga-2 flex-wrap" style="min-width: 200px">
          <v-icon icon="mdi-history" size="18" class="text-medium-emphasis" />
          <span class="text-caption text-medium-emphasis">{{ scanSummary }}</span>
        </div>
        <span v-if="dirty" class="text-caption text-warning d-flex align-center ga-1 mr-1">
          <v-icon icon="mdi-circle-medium" size="12" />有未保存的修改
        </span>
        <v-btn variant="text" size="small" rounded="lg" prepend-icon="mdi-refresh" @click="loadStatus">刷新</v-btn>
        <v-btn color="secondary" variant="tonal" rounded="lg" prepend-icon="mdi-play-circle-outline" :loading="scanning" @click="manualScan">
          立即扫描
        </v-btn>
        <v-btn
          color="primary"
          :variant="dirty ? 'flat' : 'tonal'"
          :elevation="dirty ? 4 : 0"
          rounded="lg"
          prepend-icon="mdi-content-save"
          @click="save"
        >
          保存
        </v-btn>
      </div>
    </div>
  </v-form>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import SectionCard from "./SectionCard.vue";
import SwitchRow from "./SwitchRow.vue";

const props = defineProps({
  pluginId: { type: String, required: true },
  initialConfig: { type: Object, default: () => ({}) },
  api: { type: Object, default: null },
});
const emit = defineEmits(["save"]);

const filterModes = [
  { title: "黑名单（BlackList）", value: "BlackList" },
  { title: "白名单（WhiteList）", value: "WhiteList" },
];

const defaultForm = () => ({
  enabled: false,
  cookies: "",
  source_paths: "/影视库/电视剧",
  dest_strm: "/data/strm",
  moviepilot_address: "",
  cron: "0 4 * * *",
  video_extensions: "mp4,mkv,ts,iso,avi,wmv,flv,rmvb,m2ts,mov,webm,mpg,mpeg,3gp,vob",
  delete_strm: false,
  warp_enabled: false,
  warp_port: "9096",
  warp_mediaserver: "",
  media_strm_path: "/data",
  httpstrm_enabled: true,
  httpstrm_final_url: true,
  aliststrm_enabled: false,
  alist_list: "",
  video_together: false,
  client_filter_enabled: false,
  client_filter_mode: "BlackList",
  client_filter_list: "Fileball\nInfuse",
});

const form = ref({ ...defaultForm(), ...(props.initialConfig || {}) });
// 打开时的快照，用来判断有没有没保存的修改
const snapshot = ref(JSON.stringify(form.value));
const dirty = computed(() => JSON.stringify(form.value) !== snapshot.value);

const status = ref(null);
const scanning = ref(false);
const active = ref("sec-cloud");

const cookieMasked = computed(() => {
  const c = (props.initialConfig || {}).cookies || "";
  return c.length > 20;
});

const sourceCount = computed(() =>
  String(form.value.source_paths || "")
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean).length,
);

/** 把常见的 5 段 cron 翻译成一句人话，看不懂就用默认提示。 */
const cronHint = computed(() => {
  const parts = String(form.value.cron || "").trim().split(/\s+/);
  if (parts.length !== 5) return "默认每天 04:00";
  const [min, hour, dom, mon, dow] = parts;
  if (!/^\d{1,2}$/.test(hour) || !/^\d{1,2}$/.test(min)) return "默认每天 04:00";
  const time = `${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;
  if (dom === "*" && mon === "*" && dow === "*") return `每天 ${time}`;
  if (dom === "*" && mon === "*" && /^\d$/.test(dow)) {
    const wd = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][Number(dow)];
    return wd ? `每${wd} ${time}` : "默认每天 04:00";
  }
  return "默认每天 04:00";
});

const scanSummary = computed(() => {
  const last = status.value?.last_scan;
  if (!last || !last.time) return "尚无扫描记录";
  const parts = [last.time, `云端 ${last.cloud_files ?? 0}`, `新增 ${last.created ?? 0}`, `已存在 ${last.skipped ?? 0}`];
  if (last.deleted) parts.push(`删除 ${last.deleted}`);
  return parts.join(" · ");
});

/** 每一步的完成度：ok 已配好，warn 缺关键项，none 不适用或未开启。 */
function stateOf(id) {
  const f = form.value;
  switch (id) {
    case "sec-cloud":
      return cookieMasked.value || f.cookies.length > 20 ? "ok" : "warn";
    case "sec-scan":
      return sourceCount.value && f.dest_strm ? "ok" : "warn";
    case "sec-plan":
      return f.cron ? "ok" : "warn";
    case "sec-warp":
      return !f.warp_enabled ? "none" : f.warp_mediaserver ? "ok" : "warn";
    case "sec-warp-redirect":
      return f.httpstrm_enabled || f.aliststrm_enabled ? "ok" : "warn";
    default:
      return "none";
  }
}

/** 左侧导航：四个分区，MediaWarp 开启后追加两个子分区。 */
const navItems = computed(() => {
  const items = [
    { id: "sec-cloud", step: 1, icon: "mdi-account-key-outline", label: "115 连接" },
    { id: "sec-scan", step: 2, icon: "mdi-folder-search-outline", label: "扫描与输出" },
    { id: "sec-plan", step: 3, icon: "mdi-cog-outline", label: "链接与计划" },
    { id: "sec-warp", step: 4, icon: "mdi-shuffle-variant", label: "MediaWarp" },
  ];
  if (form.value.warp_enabled) {
    items.push({ id: "sec-warp-redirect", icon: "mdi-fast-forward-outline", label: "直链重定向", sub: true });
    items.push({ id: "sec-warp-extra", icon: "mdi-play-box-multiple-outline", label: "播放增强", sub: true });
  }
  return items.map((it) => ({ ...it, state: it.sub ? "none" : stateOf(it.id) }));
});

function scrollTo(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}

let ticking = false;
function updateActive() {
  ticking = false;
  const OFFSET = 140;
  let current = navItems.value[0].id;
  for (const item of navItems.value) {
    const el = document.getElementById(item.id);
    if (el && el.getBoundingClientRect().top <= OFFSET) current = item.id;
  }
  active.value = current;
}

// capture：配置面板在嵌套滚动容器里，window 的 capture 监听同样能收到
function onScroll() {
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(updateActive);
}

async function loadStatus() {
  if (!props.api) return;
  try {
    status.value = await props.api.get(`plugin/${props.pluginId}/status`);
  } catch (e) {
    status.value = null;
  }
}

async function manualScan() {
  if (!props.api) return;
  scanning.value = true;
  try {
    await props.api.post(`plugin/${props.pluginId}/manual_scan`);
    setTimeout(loadStatus, 5000);
  } catch (e) {
    /* 错误由框架提示 */
  } finally {
    setTimeout(() => (scanning.value = false), 2000);
  }
}

function save() {
  snapshot.value = JSON.stringify(form.value);
  emit("save", { ...form.value });
}

onMounted(() => {
  loadStatus();
  window.addEventListener("scroll", onScroll, true);
  updateActive();
});
onBeforeUnmount(() => window.removeEventListener("scroll", onScroll, true));
</script>

<style scoped>
.sm-config {
  --sm-radius: 16px;
  --sm-border: rgba(var(--v-theme-on-surface), 0.1);
  --sm-line: rgba(var(--v-theme-on-surface), 0.08);
  --sm-primary: rgb(var(--v-theme-primary));
  --sm-success: rgb(var(--v-theme-success));
  --sm-warning: rgb(var(--v-theme-warning));
}

/* ---------- 顶部 ---------- */
.hero {
  position: relative;
  overflow: hidden;
  background: linear-gradient(120deg, var(--sm-primary) 0%, rgb(var(--v-theme-secondary)) 100%);
  color: rgb(var(--v-theme-on-primary));
}
.hero--off {
  background: rgba(var(--v-theme-on-surface), 0.06);
  color: rgb(var(--v-theme-on-surface));
}
.hero__glow {
  position: absolute;
  right: -60px;
  top: -120px;
  width: 320px;
  height: 320px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.14);
  pointer-events: none;
}
.hero__inner {
  position: relative;
}
.hero__icon {
  background: rgba(255, 255, 255, 0.22);
  backdrop-filter: blur(6px);
}
.hero__sub {
  opacity: 0.82;
}
.hero__pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
  background: rgba(255, 255, 255, 0.2);
}
.hero--off .hero__pill {
  background: rgba(var(--v-theme-on-surface), 0.08);
}
.hero__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.5);
}
.hero--off .hero__dot {
  background: rgba(var(--v-theme-on-surface), 0.35);
}
.hero__dot--on {
  background: #c8f7c5;
  box-shadow: 0 0 0 3px rgba(200, 247, 197, 0.3);
}
.hero__switch {
  cursor: pointer;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.18);
  transition: background 0.15s ease;
}
.hero__switch:hover {
  background: rgba(255, 255, 255, 0.28);
}
.hero--off .hero__switch {
  background: rgba(var(--v-theme-on-surface), 0.06);
}
.hero--off .hero__switch:hover {
  background: rgba(var(--v-theme-on-surface), 0.1);
}

/* ---------- 左侧导航 ---------- */
.nav-wrap {
  position: sticky;
  top: 12px;
}
.nav {
  background: rgb(var(--v-theme-surface));
  border: 1px solid var(--sm-border);
  border-radius: var(--sm-radius);
  padding-bottom: 6px;
}
.nav__caption {
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: rgba(var(--v-theme-on-surface), 0.45);
}
.nav__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 2px 6px;
  padding: 9px 10px;
  border-radius: 12px;
  cursor: pointer;
  color: rgba(var(--v-theme-on-surface), 0.7);
  font-size: 0.875rem;
  white-space: nowrap;
  transition: background 0.15s ease, color 0.15s ease;
}
.nav__item:hover {
  background: rgba(var(--v-theme-on-surface), 0.05);
}
.nav__item--active {
  background: rgba(var(--v-theme-primary), 0.1);
  color: var(--sm-primary);
  font-weight: 600;
}
.nav__item--sub {
  padding-left: 14px;
  font-size: 0.8125rem;
}
.nav__item--sub::before {
  content: "";
  position: absolute;
  left: 0;
  top: 4px;
  bottom: 4px;
  width: 2px;
  border-radius: 2px;
  background: var(--sm-border);
}
.nav__item--sub.nav__item--active::before {
  background: var(--sm-primary);
}
.nav__num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 7px;
  font-size: 0.72rem;
  font-weight: 700;
  background: rgba(var(--v-theme-on-surface), 0.08);
  color: rgba(var(--v-theme-on-surface), 0.6);
  flex: 0 0 auto;
}
.nav__num--ok {
  background: rgba(var(--v-theme-success), 0.14);
  color: var(--sm-success);
}
.nav__item--active .nav__num {
  background: var(--sm-primary);
  color: rgb(var(--v-theme-surface));
}
.nav__tick {
  width: 14px;
  flex: 0 0 auto;
}
.nav__flag {
  margin-left: auto;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--sm-warning);
}

/* ---------- 卡片内 ---------- */
.mono :deep(input),
.mono :deep(textarea) {
  font-family: "JetBrains Mono", "Fira Code", Menlo, Consolas, monospace;
  font-size: 0.8125rem;
  letter-spacing: 0;
}
.code :deep(textarea) {
  line-height: 1.5;
}
.subblock {
  border: 1px solid var(--sm-border);
  border-radius: 14px;
  padding: 4px 16px 0;
  background: rgba(var(--v-theme-on-surface), 0.015);
}
.subblock__head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 12px 0 6px;
  border-bottom: 1px solid var(--sm-line);
}
.tracking-wide {
  letter-spacing: 0.04em;
}
.indent {
  margin: 0 0 2px 14px;
  padding-left: 14px;
  border-left: 2px solid rgba(var(--v-theme-primary), 0.25);
}

/* ---------- 底部操作栏 ---------- */
.actions-wrap {
  position: sticky;
  bottom: 8px;
  z-index: 2;
  margin-top: 16px;
}
.actions {
  background: rgb(var(--v-theme-surface));
  border: 1px solid var(--sm-border);
  border-radius: 18px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
}

/* ---------- 窄屏 ---------- */
@media (max-width: 959px) {
  .nav-wrap {
    position: static;
    margin-bottom: 16px;
  }
  .nav {
    display: flex;
    overflow-x: auto;
    padding: 6px;
  }
  .nav__caption {
    display: none;
  }
  .nav__item {
    flex: 0 0 auto;
  }
  .nav__item--sub {
    padding-left: 10px;
  }
  .nav__item--sub::before {
    display: none;
  }
  .nav__tick {
    display: none;
  }
  .nav__flag {
    margin-left: 0;
  }
}
</style>
