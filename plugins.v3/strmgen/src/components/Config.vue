<template>
  <v-form class="sm-config">
    <!-- 顶部：名称、一句话说明、总开关 -->
    <v-sheet rounded="xl" class="hero pa-5 mb-4" :class="{ 'hero--off': !form.enabled }">
      <div class="d-flex align-center ga-4 flex-wrap">
        <v-avatar size="48" rounded="xl" class="hero__icon">
          <v-icon icon="mdi-cloud-sync-outline" size="26" />
        </v-avatar>
        <div class="flex-grow-1" style="min-width: 200px">
          <div class="text-h6 font-weight-bold">Strm Media</div>
          <div class="text-body-2 hero__sub">115 目录 → 本地 STRM → 播放时经 MediaWarp 302 直链</div>
        </div>
        <div class="hero__switch d-flex align-center ga-3 px-4 py-2 rounded-pill">
          <span class="text-body-2 font-weight-medium">{{ form.enabled ? "插件已启用" : "插件未启用" }}</span>
          <v-switch v-model="form.enabled" color="success" hide-details density="compact" inset />
        </div>
      </div>
    </v-sheet>

    <v-row>
      <!-- 左侧分区导航（窄屏时变为顶部横向页签） -->
      <v-col cols="12" md="3" lg="2">
        <nav class="nav-wrap">
          <v-list class="nav py-1 px-1" rounded="xl" border>
            <div
              v-for="item in navItems"
              :key="item.id"
              class="nav__item"
              :class="{ 'nav__item--active': active === item.id, 'nav__item--sub': item.sub }"
              @click="scrollTo(item.id)"
            >
              <span class="nav__num">{{ item.step }}</span>
              <v-icon :icon="item.icon" size="18" />
              <span class="nav__label">{{ item.label }}</span>
            </div>
          </v-list>
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
            :label="cookieMasked ? '115 Cookie（已保存，输入新值可覆盖）' : '115 Cookie'"
            placeholder="粘贴浏览器中的 115 Cookie"
            prepend-inner-icon="mdi-cookie-outline"
            rows="3"
            auto-grow
          />
        </SectionCard>

        <!-- 2. 扫描与输出 -->
        <SectionCard id="sec-scan" icon="mdi-folder-search-outline" title="扫描与输出" subtitle="扫哪些云端目录、STRM 落到哪里" class="mb-4">
          <v-row dense>
            <v-col cols="12">
              <v-textarea
                v-model="form.source_paths"
                label="监控的 115 目录"
                placeholder="/影视库/电视剧"
                prepend-inner-icon="mdi-cloud-outline"
                rows="2"
                auto-grow
                hint="每行一个或逗号分隔，以 / 开头"
                persistent-hint
              />
            </v-col>
            <v-col cols="12" md="7">
              <v-text-field
                v-model="form.dest_strm"
                label="本地 STRM 输出目录"
                placeholder="/data/strm"
                prepend-inner-icon="mdi-file-tree-outline"
              />
            </v-col>
            <v-col cols="12" md="5">
              <v-text-field
                v-model="form.video_extensions"
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
                label="定时扫描 Cron"
                placeholder="0 4 * * *"
                prepend-inner-icon="mdi-clock-outline"
                hint="默认每天 04:00"
                persistent-hint
              />
            </v-col>
            <v-col cols="12" sm="6" md="3">
              <SwitchRow
                v-model="form.delete_strm"
                title="同步删除 STRM"
                desc="云端删了本地也删"
                color="error"
                class="pt-2"
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
          />

          <v-expand-transition>
            <div v-if="form.warp_enabled">
              <v-row dense class="mt-1">
                <v-col cols="12" sm="4">
                  <v-text-field v-model="form.warp_port" label="监听端口" placeholder="9096" prepend-inner-icon="mdi-ethernet" />
                </v-col>
                <v-col cols="12" sm="8">
                  <v-text-field
                    v-model="form.warp_mediaserver"
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
                  <v-icon icon="mdi-fast-forward-outline" size="16" />
                  <span class="text-caption font-weight-bold text-medium-emphasis">直链重定向</span>
                </div>
                <SwitchRow v-model="form.httpstrm_enabled" title="HTTPStrm 直链重定向" desc="STRM 内容为 HTTP URL 时生效" />
                <v-expand-transition>
                  <div v-if="form.httpstrm_enabled" class="pl-6 pb-2">
                    <v-textarea
                      v-model="form.media_strm_path"
                      label="Strm 文件前缀"
                      placeholder="/data"
                      rows="1"
                      auto-grow
                      hint="每行一个；媒体服务器里符合前缀的 STRM 才会走 HTTPStrm"
                      persistent-hint
                      density="compact"
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
                  <div v-if="form.aliststrm_enabled" class="pl-6 pb-2">
                    <v-textarea
                      v-model="form.alist_list"
                      label="Alist 服务列表（YAML）"
                      rows="5"
                      auto-grow
                      hint="仅适配 Alist V3；每项含 ADDR 与 Username/Password 或 Token，及 PrefixList"
                      persistent-hint
                      density="compact"
                    />
                  </div>
                </v-expand-transition>
              </div>

              <!-- 播放增强 -->
              <div id="sec-warp-extra" class="subblock mt-4">
                <div class="subblock__head">
                  <v-icon icon="mdi-play-box-multiple-outline" size="16" />
                  <span class="text-caption font-weight-bold text-medium-emphasis">播放增强</span>
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
                  <div v-if="form.client_filter_enabled" class="pl-6 pb-2">
                    <v-row dense>
                      <v-col cols="12" sm="4">
                        <v-select v-model="form.client_filter_mode" label="过滤模式" :items="filterModes" density="compact" hide-details />
                      </v-col>
                      <v-col cols="12" sm="8">
                        <v-textarea
                          v-model="form.client_filter_list"
                          label="客户端名单"
                          placeholder="Fileball、Infuse"
                          rows="1"
                          auto-grow
                          density="compact"
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
    <v-sheet rounded="xl" border class="actions pa-3 px-4 mt-4 d-flex align-center flex-wrap ga-3">
      <div class="flex-grow-1 d-flex align-center ga-2 flex-wrap" style="min-width: 200px">
        <v-icon icon="mdi-history" size="18" class="text-medium-emphasis" />
        <span class="text-caption text-medium-emphasis">{{ scanSummary }}</span>
      </div>
      <v-btn variant="text" size="small" prepend-icon="mdi-refresh" @click="loadStatus">刷新</v-btn>
      <v-btn color="secondary" variant="tonal" prepend-icon="mdi-play-circle-outline" :loading="scanning" @click="manualScan">
        立即扫描
      </v-btn>
      <v-btn color="primary" variant="flat" prepend-icon="mdi-content-save" @click="save">保存</v-btn>
    </v-sheet>
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
const status = ref(null);
const scanning = ref(false);
const active = ref("sec-cloud");

const cookieMasked = computed(() => {
  const c = (props.initialConfig || {}).cookies || "";
  return c.length > 20;
});

const scanSummary = computed(() => {
  const last = status.value?.last_scan;
  if (!last || !last.time) return "尚无扫描记录";
  const parts = [last.time, `云端 ${last.cloud_files ?? 0}`, `新增 ${last.created ?? 0}`, `已存在 ${last.skipped ?? 0}`];
  if (last.deleted) parts.push(`删除 ${last.deleted}`);
  return parts.join(" · ");
});

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
  return items;
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
.hero {
  background: linear-gradient(135deg, rgb(var(--v-theme-primary)) 0%, rgb(var(--v-theme-secondary)) 100%);
  color: rgb(var(--v-theme-on-primary));
}
.hero--off {
  background: rgba(var(--v-theme-on-surface), 0.06);
  color: rgb(var(--v-theme-on-surface));
}
.hero__icon,
.hero__switch {
  background: rgba(255, 255, 255, 0.35);
}
.hero__sub {
  opacity: 0.8;
}

.nav-wrap {
  position: sticky;
  top: 12px;
}
.nav {
  background: rgb(var(--v-theme-surface));
}
.nav__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 12px;
  cursor: pointer;
  color: rgba(var(--v-theme-on-surface), 0.72);
  font-size: 0.875rem;
  white-space: nowrap;
  transition: background 0.15s ease, color 0.15s ease;
}
.nav__item:hover {
  background: rgba(var(--v-theme-on-surface), 0.05);
}
.nav__item--active {
  background: rgba(var(--v-theme-primary), 0.12);
  color: rgb(var(--v-theme-primary));
  font-weight: 600;
}
.nav__item--sub {
  padding-left: 34px;
}
.nav__num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 6px;
  font-size: 0.7rem;
  font-weight: 700;
  background: rgba(var(--v-theme-on-surface), 0.08);
}
.nav__item--active .nav__num {
  background: rgb(var(--v-theme-primary));
  color: rgb(var(--v-theme-surface));
}

.subblock {
  border: 1px solid rgba(var(--v-theme-on-surface), 0.1);
  border-radius: 14px;
  padding: 4px 16px 0;
}
.subblock__head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 0 2px;
  letter-spacing: 0.04em;
}

.actions {
  position: sticky;
  bottom: 8px;
  z-index: 2;
  background: rgb(var(--v-theme-surface));
}

/* 窄屏：导航变成顶部横向页签 */
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
  .nav__item {
    flex: 0 0 auto;
  }
  .nav__item--sub {
    padding-left: 12px;
  }
}
</style>
