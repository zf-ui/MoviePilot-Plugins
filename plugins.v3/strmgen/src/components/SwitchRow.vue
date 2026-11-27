<template>
  <div
    class="switch-row d-flex align-center ga-4"
    :class="{ 'switch-row--bordered': bordered, 'switch-row--boxed': boxed }"
  >
    <div class="flex-grow-1">
      <div class="text-body-2 font-weight-medium">{{ title }}</div>
      <div v-if="desc" class="text-caption text-medium-emphasis">{{ desc }}</div>
    </div>
    <v-switch
      :model-value="modelValue"
      :color="color || 'primary'"
      :disabled="disabled"
      hide-details
      density="compact"
      inset
      @update:model-value="$emit('update:modelValue', $event)"
    />
  </div>
</template>

<script setup>
defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, required: true },
  desc: { type: String, default: "" },
  color: { type: String, default: "primary" },
  disabled: { type: Boolean, default: false },
  bordered: { type: Boolean, default: false },
  /** 做成独立的圆角小块，和旁边的输入框等高对齐。 */
  boxed: { type: Boolean, default: false },
});
defineEmits(["update:modelValue"]);
</script>

<style scoped>
.switch-row {
  padding: 8px 0;
  min-height: 48px;
}
.switch-row--bordered {
  border-top: 1px solid rgba(var(--v-theme-on-surface), 0.08);
}
.switch-row--boxed {
  padding: 4px 14px;
  min-height: 56px;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.1);
  border-radius: 12px;
  transition: border-color 0.15s ease;
}
.switch-row--boxed:hover {
  border-color: rgba(var(--v-theme-on-surface), 0.25);
}
</style>
