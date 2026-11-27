<template>
  <v-card variant="flat" class="strmgen-page">
    <v-card-title class="d-flex align-center ga-2 pb-1">
      <v-icon icon="mdi-cloud-download-outline" color="primary" />
      strm生成 · 运行状态
      <v-spacer />
      <v-chip v-if="config" :color="config.enabled ? 'success' : 'grey'" size="small" variant="tonal">
        <v-icon start :icon="config.enabled ? 'mdi-check-circle' : 'mdi-pause-circle'" />
        {{ config.enabled ? "已启用" : "未启用" }}
      </v-chip>
      <v-tooltip text="刷新状态">
        <template #activator="{ props: tooltipProps }">
          <v-btn v-bind="tooltipProps" icon="mdi-refresh" size="small" variant="text" :loading="loading" @click="loadStatus" />
        </template>
      </v-tooltip>
      <v-tooltip text="立即执行一次扫描">
        <template #activator="{ props: tooltipProps }">
          <v-btn
            v-bind="tooltipProps"
            icon="mdi-play-circle-outline"
            size="small"
            color="primary"
            variant="tonal"
            :loading="scanning"
            @click="manualScan"
          />
        </template>
      </v-tooltip>
    </v-card-title>
    <v-divider class="mb-3" />

    <v-card-text v-if="config" class="pt-0">
      <v-row dense>
        <v-col v-for="item in configItems" :key="item.label" cols="12" sm="6" md="3">
          <v-sheet rounded="lg" class="pa-3 config-item">
            <div class="d-flex align-center ga-2 mb-1">
              <v-icon size="18" :icon="item.icon" color="primary" />
              <div class="text-caption text-medium-emphasis">{{ item.label }}</div>
            </div>
            <div class="text-body-2 font-weight-medium text-truncate" :title="item.value">
              {{ item.value || "未配置" }}
            </div>
          </v-sheet>
        </v-col>
      </v-row>
    </v-card-text>
    <v-card-text v-else class="text-caption text-medium-emphasis pt-0">
      {{ loading ? "正在加载状态…" : "无法获取状态，请刷新重试" }}
    </v-card-text>

    <template v-if="lastScan">
      <v-divider class="my-1" />
      <v-card-text class="pt-3">
        <div class="d-flex align-center ga-2 mb-3">
          <v-icon icon="mdi-chart-box-outline" size="20" />
          <span class="text-subtitle-1 font-weight-medium">最近扫描</span>
          <v-chip size="small" variant="text" label class="text-medium-emphasis">
            <v-icon start icon="mdi-clock-outline" size="14" />
            {{ lastScan.time || "-" }}
          </v-chip>
          <v-spacer />
          <v-chip v-if="lastScan.error" color="error" size="small" variant="tonal">
            <v-icon start icon="mdi-alert-circle-outline" size="14" /> 出错
          </v-chip>
          <v-chip v-else color="success" size="small" variant="tonal">
            <v-icon start icon="mdi-check-circle-outline" size="14" /> 成功
          </v-chip>
        </div>

        <v-row dense>
          <v-col v-for="stat in scanStats" :key="stat.label" cols="6" md="3">
            <v-sheet rounded="lg" class="pa-3 stat-card">
              <div class="d-flex align-center ga-2">
                <v-avatar size="36" rounded="lg" :color="stat.color" variant="tonal">
                  <v-icon :icon="stat.icon" />
                </v-avatar>
                <div>
                  <div class="text-h6">{{ stat.value }}</div>
                  <div class="text-caption text-medium-emphasis">{{ stat.label }}</div>
                </div>
              </div>
            </v-sheet>
          </v-col>
        </v-row>

        <template v-if="sourceLines.length">
          <div class="text-subtitle-2 mt-4 mb-1">分目录明细</div>
          <v-sheet rounded="lg" border class="pa-2">
            <div v-for="line in sourceLines" :key="line" class="d-flex ga-2 align-center text-body-2 py-1">
              <v-icon icon="mdi-folder-outline" size="16" color="medium-emphasis" />
              <span class="text-medium-emphasis">{{ line }}</span>
            </div>
          </v-sheet>
        </template>

        <v-alert
          v-if="lastScan.error"
          type="error"
          variant="tonal"
          density="compact"
          class="mt-3"
          :text="lastScan.error"
        />
      </v-card-text>
    </template>
    <v-card-text v-else class="text-caption text-medium-emphasis">
      <v-icon icon="mdi-information-outline" size="14" class="mr-1" />
      尚无扫描记录，点击右上角播放按钮立即扫描
    </v-card-text>
  </v-card>
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

const config = computed(() => data.value?.config || null);
const lastScan = computed(() => data.value?.last_scan || null);

const configItems = computed(() => {
  if (!config.value) return [];
  return [
    { label: "监控目录", value: config.value.source_paths, icon: "mdi-folder-multiple-outline" },
    { label: "输出目录", value: config.value.dest_strm, icon: "mdi-file-tree-outline" },
    { label: "定时计划", value: config.value.cron, icon: "mdi-clock-outline" },
    { label: "链接前缀", value: config.value.moviepilot_address, icon: "mdi-link-variant" },
  ];
});

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
  return sources.map(
    (s) =>
      `${s.source}：云端 ${s.cloud_files ?? 0}，新增 ${s.created ?? 0}，` +
      `已存在 ${s.skipped ?? 0}${s.error ? `，错误：${s.error}` : ""}`
  );
});

async function loadStatus() {
  if (!props.api) return;
  loading.value = true;
  try {
    data.value = await props.api.get(`plugin/${props.pluginId}/status`);
  } catch (e) {
    data.value = null;
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
.config-item {
  background: rgba(var(--v-theme-on-surface), 0.04);
  min-height: 72px;
}
.stat-card {
  background: rgba(var(--v-theme-on-surface), 0.04);
}
</style>
