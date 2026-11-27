<template>
  <v-form class="strmgen-config">
    <!-- 主开关横幅 -->
    <v-sheet rounded="xl" class="hero pa-5 mb-4" :class="{ 'hero--off': !form.enabled }">
      <div class="d-flex align-center ga-4 flex-wrap">
        <v-avatar size="48" rounded="xl" class="hero__icon">
          <v-icon icon="mdi-cloud-sync-outline" size="26" />
        </v-avatar>
        <div class="flex-grow-1" style="min-width: 200px">
          <div class="text-h6 font-weight-bold">strm生成</div>
          <div class="text-body-2 hero__sub">监控 115 网盘目录，定时生成 STRM；播放时经 302 接口实时取直链</div>
        </div>
        <div class="hero__switch d-flex align-center ga-3 px-4 py-2 rounded-pill">
          <span class="text-body-2 font-weight-medium">{{ form.enabled ? "插件已启用" : "插件未启用" }}</span>
          <v-switch v-model="form.enabled" color="success" hide-details density="compact" inset />
        </div>
      </div>
    </v-sheet>

    <v-row>
      <v-col cols="12" md="5">
        <SectionCard icon="mdi-account-key-outline" title="网盘连接" subtitle="115 网盘身份凭证，用于列目录与取直链" class="fill-height">
          <template #action>
            <v-chip size="small" label variant="tonal" :color="cookieMasked ? 'success' : 'warning'">
              {{ cookieMasked ? "已保存" : "未配置" }}
            </v-chip>
          </template>
          <v-textarea
            v-model="form.cookies"
            :label="cookieMasked ? '115 Cookie（已保存，输入新值可覆盖）' : '115 Cookie'"
            placeholder="粘贴浏览器中的 115 Cookie"
            rows="4"
            auto-grow
          />
        </SectionCard>
      </v-col>

      <v-col cols="12" md="7">
        <SectionCard icon="mdi-folder-search-outline" title="扫描范围" subtitle="扫哪里、输出到哪里、认哪些扩展名">
          <v-textarea
            v-model="form.source_paths"
            label="监控的 115 目录"
            placeholder="/影视库/电视剧"
            rows="2"
            auto-grow
            hint="每行一个或逗号分隔，以 / 开头"
            persistent-hint
            class="mb-1"
          />
          <v-text-field
            v-model="form.dest_strm"
            label="本地 STRM 输出目录"
            placeholder="/data/strm"
            prepend-inner-icon="mdi-file-tree-outline"
          />
          <v-text-field
            v-model="form.video_extensions"
            label="视频扩展名"
            placeholder="mp4,mkv,ts"
            prepend-inner-icon="mdi-file-video-outline"
            hint="逗号分隔"
            persistent-hint
          />
        </SectionCard>
      </v-col>

      <v-col cols="12">
        <SectionCard icon="mdi-cog-outline" title="运行设置" subtitle="链接前缀、定时计划与删除策略">
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
                desc="云端删了本地也删，谨慎开启"
                color="error"
                class="pt-3"
              />
            </v-col>
          </v-row>
        </SectionCard>
      </v-col>

      <v-col cols="12">
        <SectionCard icon="mdi-shuffle-variant" title="MediaWarp 中间件" subtitle="直链重定向、共同观影与客户端黑白名单" class="mb-0">
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
              <v-row dense class="mt-2">
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

              <div class="features mt-4">
                <SwitchRow v-model="form.httpstrm_enabled" title="HTTPStrm 直链重定向" desc="Strm 内容为 HTTP URL 时生效" />
                <v-expand-transition>
                  <div v-if="form.httpstrm_enabled" class="pl-6 pb-2">
                    <v-textarea
                      v-model="form.media_strm_path"
                      label="Strm 文件前缀"
                      placeholder="/data"
                      rows="1"
                      auto-grow
                      hint="每行一个；符合前缀的 Strm 才会走 HTTPStrm"
                      persistent-hint
                      density="compact"
                    />
                    <SwitchRow v-model="form.httpstrm_final_url" title="FinalURL" desc="重定向到最终直链，适合公网播放局域网地址" />
                  </div>
                </v-expand-transition>

                <SwitchRow v-model="form.aliststrm_enabled" title="AlistStrm 直链重定向" desc="Strm 内容为 Alist 文件路径时生效" bordered />
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

                <SwitchRow v-model="form.video_together" title="共同观影" desc="VideoTogether，网页端同步播放进度" bordered />
                <SwitchRow v-model="form.client_filter_enabled" title="客户端黑白名单" desc="只对名单内 / 外的客户端生效" color="warning" bordered />
                <v-expand-transition>
                  <div v-if="form.client_filter_enabled" class="pl-6 pb-2">
                    <v-row dense>
                      <v-col cols="12" sm="4">
                        <v-select
                          v-model="form.client_filter_mode"
                          label="过滤模式"
                          :items="filterModes"
                          density="compact"
                          hide-details
                        />
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

    <!-- 操作栏 -->
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
import { computed, onMounted, ref } from "vue";
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

<style scoped>
.hero {
  background: linear-gradient(135deg, rgb(var(--v-theme-primary)) 0%, rgb(var(--v-theme-secondary)) 100%);
  color: rgb(var(--v-theme-on-primary));
}
.hero--off {
  background: linear-gradient(135deg, rgba(var(--v-theme-on-surface), 0.45), rgba(var(--v-theme-on-surface), 0.25));
  color: rgb(var(--v-theme-on-surface));
}
.hero__icon,
.hero__switch {
  background: rgba(255, 255, 255, 0.18);
}
.hero__sub {
  opacity: 0.85;
}
.features {
  border: 1px solid rgba(var(--v-theme-on-surface), 0.08);
  border-radius: 12px;
  padding: 0 16px;
}
.actions {
  position: sticky;
  bottom: 8px;
  background: rgb(var(--v-theme-surface));
}
</style>
