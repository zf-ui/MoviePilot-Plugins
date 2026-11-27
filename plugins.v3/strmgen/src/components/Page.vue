<template>
  <div class="strmgen-page">
    <!-- 顶部横幅 -->
    <v-sheet rounded="xl" class="hero pa-5 mb-4 elevation-0" :class="{ 'hero--off': config && !config.enabled }">
      <div class="d-flex flex-wrap align-center ga-4">
        <v-avatar size="56" rounded="xl" class="hero__icon">
          <v-icon icon="mdi-cloud-sync-outline" size="30" />
        </v-avatar>
        <div class="flex-grow-1 hero__text" style="min-width: 220px">
          <div class="d-flex align-center flex-wrap ga-2">
            <span class="text-h5 font-weight-bold">strm生成</span>
            <v-chip
              v-if="config"
              size="small"
              label
              variant="flat"
              :color="config.enabled ? 'success' : 'warning'"
              class="hero__chip"
            >
              <v-icon start :icon="config.enabled ? 'mdi-check-circle' : 'mdi-pause-circle'" size="14" />
              {{ config.enabled ? "运行中" : "已暂停" }}
            </v-chip>
          </div>
          <div class="text-body-2 mt-1 hero__sub">
            115 云盘目录 → 本地 STRM → 播放时实时 302 直链
          </div>
        </div>
        <div class="d-flex ga-2">
          <v-btn variant="text" class="hero__btn" prepend-icon="mdi-refresh" :loading="loading" @click="loadStatus">
            刷新
          </v-btn>
          <v-btn color="primary" variant="flat" prepend-icon="mdi-play-outline" :loading="scanning" @click="manualScan">
            立即扫描
          </v-btn>
        </div>
      </div>
    </v-sheet>

    <v-alert
      v-if="loadError"
      type="error"
      variant="tonal"
      density="compact"
      rounded="lg"
      class="mb-4"
      text="无法获取插件状态，请刷新重试。"
    />

    <!-- 概览 -->
    <v-row dense class="mb-4">
      <v-col v-for="item in overview" :key="item.label" cols="6" sm="4" md="2">
        <v-sheet rounded="lg" border class="tile pa-3 fill-height">
          <div class="d-flex align-center ga-2 mb-1">
            <v-avatar size="24" rounded="circle" :color="item.color" variant="tonal">
              <v-icon :icon="item.icon" size="14" />
            </v-avatar>
            <span class="text-caption text-medium-emphasis">{{ item.label }}</span>
          </div>
          <div class="text-body-2 font-weight-bold text-truncate" :title="item.value">{{ item.value }}</div>
          <div v-if="item.sub" class="text-caption text-medium-emphasis text-truncate" :title="item.sub">
            {{ item.sub }}
          </div>
        </v-sheet>
      </v-col>
    </v-row>

    <!-- 流程 -->
    <v-sheet rounded="xl" border class="pipeline pa-4 mb-4">
      <div class="d-flex align-center">
        <template v-for="(step, i) in pipeline" :key="step.title">
          <div class="pipeline__step d-flex align-center ga-3 px-2 py-1">
            <v-avatar size="36" rounded="lg" :color="step.color" variant="tonal">
              <v-icon :icon="step.icon" size="20" />
            </v-avatar>
            <div>
              <div class="text-body-2 font-weight-medium">{{ step.title }}</div>
              <div class="text-caption text-medium-emphasis">{{ step.desc }}</div>
            </div>
          </div>
          <v-icon v-if="i < pipeline.length - 1" icon="mdi-chevron-right" class="pipeline__arrow flex-grow-1" />
        </template>
      </div>
    </v-sheet>

    <!-- 最近扫描 -->
    <v-sheet rounded="xl" border class="pa-5">
      <div class="d-flex align-center flex-wrap ga-3 mb-4">
        <v-avatar size="40" rounded="lg" color="secondary" variant="tonal">
          <v-icon icon="mdi-chart-box-outline" size="22" />
        </v-avatar>
        <div class="flex-grow-1">
          <div class="text-subtitle-1 font-weight-bold">最近扫描</div>
          <div class="text-caption text-medium-emphasis">
            {{ lastScan ? (lastScan.time || "时间未知") : "尚无扫描记录" }}
          </div>
        </div>
        <v-chip v-if="lastScan" :color="lastScan.error ? 'error' : 'success'" size="small" variant="tonal" label>
          <v-icon start :icon="lastScan.error ? 'mdi-alert-circle-outline' : 'mdi-check-circle-outline'" size="14" />
          {{ lastScan.error ? "出错" : "成功" }}
        </v-chip>
      </div>

      <template v-if="lastScan">
        <v-row dense>
          <v-col v-for="stat in scanStats" :key="stat.key" cols="6" md="3">
            <v-sheet rounded="lg" border class="stat pa-4">
              <div class="stat__bar" :style="{ background: `rgb(var(--v-theme-${stat.color}))` }" />
              <div class="d-flex align-center justify-space-between">
                <div>
                  <div class="text-h4 font-weight-bold">{{ stat.value }}</div>
                  <div class="text-caption text-medium-emphasis">{{ stat.label }}</div>
                </div>
                <v-avatar size="40" rounded="lg" :color="stat.color" variant="tonal">
                  <v-icon :icon="stat.icon" />
                </v-avatar>
              </div>
            </v-sheet>
          </v-col>
        </v-row>

        <v-alert v-if="lastScan.error" type="error" variant="tonal" density="compact" rounded="lg" class="mt-4" :text="lastScan.error" />

        <template v-if="sourceLines.length">
          <div class="text-subtitle-2 font-weight-medium mt-5">分目录明细</div>
          <div>
            <div v-for="line in sourceLines" :key="line.source" class="source-row py-3">
              <div class="d-flex align-center ga-2 flex-wrap">
                <v-icon icon="mdi-folder-outline" size="18" color="medium-emphasis" />
                <span class="text-body-2 font-weight-medium text-truncate flex-grow-1" :title="line.source">{{ line.source }}</span>
                <v-chip size="small" variant="tonal" color="primary" label>云端 {{ line.cloud }}</v-chip>
                <v-chip size="small" variant="tonal" color="success" label>新增 {{ line.created }}</v-chip>
                <v-chip size="small" variant="tonal" color="info" label>已存在 {{ line.skipped }}</v-chip>
                <v-chip v-if="line.deleted" size="small" variant="tonal" color="error" label>删除 {{ line.deleted }}</v-chip>
              </div>
              <div v-if="line.error" class="text-caption text-error mt-1">{{ line.error }}</div>
              <v-progress-linear
                :model-value="line.ratio"
                height="4"
                rounded
                color="success"
                bg-color="rgba(var(--v-theme-on-surface), 0.1)"
                bg-opacity="1"
                class="mt-2"
              />
            </div>
          </div>
        </template>
      </template>

      <v-sheet v-else rounded="lg" class="empty pa-8 text-center">
        <v-avatar size="56" rounded="xl" color="primary" variant="tonal" class="mb-3">
          <v-icon icon="mdi-radar" size="28" />
        </v-avatar>
        <div class="text-body-1 font-weight-medium">还没有扫描记录</div>
        <div class="text-caption text-medium-emphasis mb-3">点击「立即扫描」马上跑一次，结果会显示在这里</div>
        <v-btn size="small" color="primary" variant="tonal" prepend-icon="mdi-play-outline" :loading="scanning" @click="manualScan">
          立即扫描
        </v-btn>
      </v-sheet>
    </v-sheet>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";

const props = defineProps({
  pluginId: { type: String, required: true },
  api: { type: Object, default: null },
});

const data = ref(null);
const loading = ref(false);
const scanning = ref(false);
const loadError = ref(false);

const config = computed(() => data.value?.config || null);
const lastScan = computed(() => data.value?.last_scan || null);

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/** 把 5 段 cron 翻译成一句人话，看不懂就返回空。 */
function describeCron(cron) {
  const parts = String(cron || "").trim().split(/\s+/);
  if (parts.length !== 5) return "";
  const [min, hour, dom, mon, dow] = parts;
  if (!/^\d{1,2}$/.test(hour) || !/^\d{1,2}$/.test(min)) return "";
  const time = `${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;
  if (dom === "*" && mon === "*" && dow === "*") return `每天 ${time}`;
  if (dom === "*" && mon === "*" && /^\d$/.test(dow)) return `每${WEEKDAYS[Number(dow)] || ""} ${time}`;
  return "";
}

const overview = computed(() => {
  const c = config.value;
  if (!c) return [];
  const sources = String(c.source_paths || "")
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
  return [
    {
      label: "定时扫描",
      value: c.cron || "未设置",
      sub: describeCron(c.cron),
      icon: "mdi-clock-outline",
      color: "primary",
    },
    {
      label: "监控目录",
      value: sources.length ? `${sources.length} 个目录` : "未配置",
      sub: sources[0] || "",
      icon: "mdi-folder-multiple-outline",
      color: "primary",
    },
    {
      label: "STRM 输出",
      value: c.dest_strm || "未配置",
      sub: c.delete_strm ? "同步删除已开启" : "",
      icon: "mdi-file-tree-outline",
      color: "secondary",
    },
    {
      label: "链接前缀",
      value: c.moviepilot_address || "系统默认",
      sub: "",
      icon: "mdi-link-variant",
      color: "secondary",
    },
    {
      label: "MediaWarp",
      value: c.warp_enabled ? `端口 ${c.warp_port}` : "未启用",
      sub: c.warp_enabled ? `上游 ${c.warp_mediaserver || "未指定"}` : "",
      icon: "mdi-shuffle-variant",
      color: c.warp_enabled ? "info" : "grey",
    },
    {
      label: "115 Cookie",
      value: c.cookies === "未配置" ? "未配置" : "已配置",
      sub: "",
      icon: "mdi-key-variant",
      color: c.cookies === "未配置" ? "warning" : "success",
    },
  ];
});

const pipeline = [
  { icon: "mdi-cloud-outline", title: "115 云盘", desc: "递归扫描视频文件", color: "primary" },
  { icon: "mdi-file-link-outline", title: "生成 STRM", desc: "落到本地输出目录", color: "primary" },
  { icon: "mdi-server-outline", title: "媒体库", desc: "Emby / Jellyfin 入库", color: "secondary" },
  { icon: "mdi-fast-forward-outline", title: "302 直链", desc: "播放时实时取链", color: "success" },
];

const scanStats = computed(() => {
  const last = lastScan.value || {};
  return [
    { key: "cloud", label: "云端视频", value: last.cloud_files ?? 0, icon: "mdi-cloud-outline", color: "primary" },
    { key: "created", label: "新增 STRM", value: last.created ?? 0, icon: "mdi-file-plus-outline", color: "success" },
    { key: "skipped", label: "已存在跳过", value: last.skipped ?? 0, icon: "mdi-file-check-outline", color: "info" },
    { key: "deleted", label: "删除", value: last.deleted ?? 0, icon: "mdi-file-remove-outline", color: "error" },
  ];
});

const sourceLines = computed(() => {
  const sources = lastScan.value?.sources || [];
  return sources.map((s) => {
    const created = s.created ?? 0;
    const skipped = s.skipped ?? 0;
    const total = created + skipped;
    return {
      source: s.source,
      cloud: s.cloud_files ?? 0,
      created,
      skipped,
      deleted: s.deleted ?? 0,
      ratio: total ? Math.round((created / total) * 100) : 0,
      error: s.error,
    };
  });
});

async function loadStatus() {
  if (!props.api) return;
  loading.value = true;
  loadError.value = false;
  try {
    const res = await props.api.get(`plugin/${props.pluginId}/status`);
    data.value = res && (res.config || res.last_scan) ? res : null;
    loadError.value = !data.value;
  } catch (e) {
    data.value = null;
    loadError.value = true;
  } finally {
    loading.value = false;
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

onMounted(loadStatus);
</script>

<style scoped>
.hero {
  background: linear-gradient(135deg, rgb(var(--v-theme-primary)) 0%, rgb(var(--v-theme-secondary)) 100%);
  color: rgb(var(--v-theme-on-primary));
}
.hero--off {
  background: linear-gradient(135deg, rgba(var(--v-theme-on-surface), 0.45), rgba(var(--v-theme-on-surface), 0.25));
  color: rgb(var(--v-theme-on-surface));
}
.hero__icon {
  background: rgba(255, 255, 255, 0.18);
}
.hero__sub {
  opacity: 0.85;
}
.hero__chip {
  color: rgb(var(--v-theme-on-surface));
}
.hero__btn {
  color: inherit;
}
.pipeline {
  overflow-x: auto;
}
.pipeline__step {
  white-space: nowrap;
}
.pipeline__arrow {
  color: rgba(var(--v-theme-on-surface), 0.35);
}
.tile {
  background: rgb(var(--v-theme-surface));
}
.stat {
  position: relative;
  overflow: hidden;
}
.stat__bar {
  position: absolute;
  top: 0;
  left: 0;
  width: 4px;
  height: 100%;
}
.source-row {
  border-top: 1px solid rgba(var(--v-theme-on-surface), 0.08);
}
.empty {
  background: rgba(var(--v-theme-on-surface), 0.03);
  border: 1px dashed rgba(var(--v-theme-on-surface), 0.2);
}
</style>
