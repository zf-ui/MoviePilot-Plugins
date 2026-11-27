<template>
  <div class="switch-row d-flex align-center ga-4 py-2" :class="{ 'switch-row--bordered': bordered }">
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
});
defineEmits(["update:modelValue"]);
</script>

<style scoped>
.switch-row--bordered {
  border-top: 1px solid rgba(var(--v-theme-on-surface), 0.08);
}
</style>
