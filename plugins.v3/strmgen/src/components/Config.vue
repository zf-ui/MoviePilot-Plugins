<template>
  <v-form class="strmgen-config">
    <v-alert variant="tonal" density="compact" color="primary" class="mb-4">
      <div class="d-flex align-start ga-2">
        <v-icon icon="mdi-cloud-download-outline" size="20" />
        <div class="text-body-2">
          监控指定 115 网盘目录，定时递归扫描并生成 STRM；播放时经本插件 302 接口实时获取 115 直链。
        </div>
      </div>
    </v-alert>

    <!-- 基础连接 -->
    <div class="d-flex align-center ga-2 mb-3">
      <v-icon icon="mdi-connection" size="18" color="primary" />
      <span class="text-subtitle-2">网盘连接</span>
    </div>
    <v-textarea
      v-model="form.cookies"
      :label="`115 Cookie${cookieMasked ? '（已配置，输入新值可覆盖）' : ''}`"
      :placeholder="cookieMasked ? '已保存 Cookie，输入新值可覆盖' : '粘贴浏览器中的 115 Cookie'"
      rows="2"
      auto-grow
      class="mb-2"
    />

    <!-- 扫描范围 -->
    <div class="d-flex align-center ga-2 mb-3 mt-4">
      <v-icon icon="mdi-folder-search-outline" size="18" color="primary" />
      <span class="text-subtitle-2">扫描范围</span>
    </div>
    <v-textarea
      v-model="form.source_paths"
      label="监控的 115 目录（每行一个，以 / 开头）"
      placeholder="/影视库/电视剧"
      rows="2"
      auto-grow
      class="mb-2"
      hint="支持多个目录，按行或逗号分隔"
      persistent-hint
    />
    <v-text-field
      v-model="form.dest_strm"
      label="本地 STRM 输出目录"
      placeholder="/data/strm"
      prepend-inner-icon="mdi-file-tree-outline"
      class="mb-2"
    />
    <v-text-field
      v-model="form.video_extensions"
      label="视频扩展名（逗号分隔）"
      prepend-inner-icon="mdi-file-video-outline"
      class="mb-2"
    />

    <!-- 运行设置 -->
    <div class="d-flex align-center ga-2 mb-3 mt-4">
      <v-icon icon="mdi-cog-outline" size="18" color="primary" />
      <span class="text-subtitle-2">运行设置</span>
    </div>
    <v-text-field
      v-model="form.moviepilot_address"
      label="MoviePilot 外部访问地址（STRM 链接前缀）"
      placeholder="http://192.168.1.10:3000"
      prepend-inner-icon="mdi-link-variant"
      hint="播放器需能访问该地址；留空使用系统 APP_DOMAIN"
      persistent-hint
      class="mb-2"
    />
    <v-text-field
      v-model="form.cron"
      label="定时扫描 Cron"
      placeholder="0 4 * * *"
      prepend-inner-icon="mdi-clock-outline"
      hint="默认每天 04:00"
      persistent-hint
      class="mb-3"
    />
    <v-switch
      v-model="form.delete_strm"
      label="同步删除云端已删除的 STRM（谨慎开启）"
      color="error"
      hide-details
      density="compact"
      class="mb-2"
    />
    <!-- MediaWarp 中间件 -->
    <div class="d-flex align-center ga-2 mb-3 mt-4">
      <v-icon icon="mdi-server-network" size="18" color="primary" />
      <span class="text-subtitle-2">MediaWarp 中间件（直链重定向 / 共同观影 / 客户端黑名单）</span>
    </div>
    <v-switch
      v-model="form.warp_enabled"
      label="启用 MediaWarp 中间件"
      color="primary"
      density="compact"
      class="mb-2"
    />
    <template v-if="form.warp_enabled">
      <v-text-field
        v-model="form.warp_port"
        label="中间件监听端口"
        placeholder="9096"
        prepend-inner-icon="mdi-numeric"
        class="mb-2"
      />
      <v-text-field
        v-model="form.warp_mediaserver"
        label="媒体服务器名称（Emby/Jellyfin，与 MoviePilot 中配置一致）"
        placeholder="Emby"
        prepend-inner-icon="mdi-server"
        hint="用于解析媒体服务器地址与 API Key；留空则不启动中间件"
        persistent-hint
        class="mb-2"
      />
      <v-switch
        v-model="form.httpstrm_enabled"
        label="启用 HTTPStrm 直链重定向（Strm 内容为 HTTP URL）"
        color="primary"
        density="compact"
        class="mb-2"
      />
      <template v-if="form.httpstrm_enabled">
        <v-switch
          v-model="form.httpstrm_final_url"
          label="FinalURL：重定向到最终直链（适用于公网播放局域网地址）"
          density="compact"
          hide-details
          class="mb-2"
        />
        <v-textarea
          v-model="form.media_strm_path"
          label="Strm 文件前缀（每行一个，符合该前缀的 Strm 才会走 HTTPStrm）"
          placeholder="/data"
          rows="1"
          auto-grow
          class="mb-2"
        />
      </template>
      <v-switch
        v-model="form.aliststrm_enabled"
        label="启用 AlistStrm 直链重定向（Strm 内容为 Alist 路径）"
        color="primary"
        density="compact"
        class="mb-2"
      />
      <template v-if="form.aliststrm_enabled">
        <v-textarea
          v-model="form.alist_list"
          label="Alist 服务列表（YAML）"
          rows="5"
          auto-grow
          class="mb-2"
          hint="仅适配 Alist V3；每项含 ADDR 与 Username/Password 或 Token，及 PrefixList；Token 优先级高于账号密码"
          persistent-hint
        />
      </template>
      <v-switch
        v-model="form.video_together"
        label="启用共同观影（VideoTogether）"
        density="compact"
        hide-details
        class="mb-2"
      />
      <v-switch
        v-model="form.client_filter_enabled"
        label="启用客户端黑白名单过滤"
        color="warning"
        density="compact"
        class="mb-2"
      />
      <template v-if="form.client_filter_enabled">
        <v-select
          v-model="form.client_filter_mode"
          label="过滤模式"
          :items="filterModes"
          density="compact"
          class="mb-2"
        />
        <v-textarea
          v-model="form.client_filter_list"
          label="客户端名单（每行一个）"
          placeholder="Fileball、Infuse"
          rows="2"
          auto-grow
          class="mb-2"
        />
      </template>
    </template>
    <v-switch
      v-model="form.enabled"
      label="启用插件"
      color="primary"
      density="compact"
      class="mb-4"
    />

    <div class="d-flex ga-3 flex-wrap">
      <v-btn color="primary" variant="flat" prepend-icon="mdi-content-save" @click="save">保存</v-btn>
      <v-btn color="secondary" variant="tonal" prepend-icon="mdi-play-circle-outline" :loading="scanning" @click="manualScan">
        立即扫描
      </v-btn>
      <v-btn variant="text" prepend-icon="mdi-refresh" @click="loadStatus">刷新状态</v-btn>
    </div>

    <v-card v-if="status" variant="tonal" class="mt-4">
      <v-card-title class="text-subtitle-1 d-flex align-center ga-2">
        <v-icon icon="mdi-chart-box-outline" size="18" />
        最近扫描
      </v-card-title>
      <v-card-text>
        <template v-if="statusText">
          <div class="text-body-2 mb-1">{{ statusText }}</div>
        </template>
        <template v-if="sourceLines.length">
          <v-divider class="my-2" />
          <div v-for="line in sourceLines" :key="line" class="d-flex ga-1 align-center text-caption text-medium-emphasis py-1">
            <v-icon icon="mdi-folder-outline" size="14" />
            {{ line }}
          </div>
        </template>
        <template v-else>
          <div class="text-caption text-medium-emphasis">尚无扫描记录</div>
        </template>
      </v-card-text>
    </v-card>
  </v-form>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";

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

const cookieMasked = computed(() => {
  const c = (props.initialConfig || {}).cookies || "";
  return c.length > 20;
});

const statusText = computed(() => {
  const last = status.value?.last_scan;
  if (!last || !last.time) return "";
  const parts = [
    `时间：${last.time}`,
    `云端视频：${last.cloud_files ?? 0}`,
    `新增：${last.created ?? 0}`,
    `已存在：${last.skipped ?? 0}`,
    `删除：${last.deleted ?? 0}`,
  ];
  if (last.error) parts.push(`错误：${last.error}`);
  return parts.join("  |  ");
});

const sourceLines = computed(() => {
  const sources = status.value?.last_scan?.sources || [];
  return sources.map(
    (s) =>
      `${s.source}：云端 ${s.cloud_files ?? 0}，新增 ${s.created ?? 0}，` +
      `已存在 ${s.skipped ?? 0}${s.error ? `，错误：${s.error}` : ""}`
  );
});

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

onMounted(loadStatus);
</script>
