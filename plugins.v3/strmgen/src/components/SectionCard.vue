<template>
  <v-sheet rounded="xl" border class="section-card pa-5">
    <div class="d-flex align-center ga-3 mb-4 flex-wrap">
      <v-avatar size="40" rounded="lg" color="primary" variant="tonal">
        <v-icon :icon="icon" size="22" />
      </v-avatar>
      <div class="flex-grow-1" style="min-width: 160px">
        <div class="text-subtitle-1 font-weight-bold">{{ title }}</div>
        <div v-if="subtitle" class="text-caption text-medium-emphasis">{{ subtitle }}</div>
      </div>
      <slot name="action" />
    </div>
    <slot />
  </v-sheet>
</template>

<script setup>
defineProps({
  icon: { type: String, required: true },
  title: { type: String, required: true },
  subtitle: { type: String, default: "" },
});
</script>

<style scoped>
.section-card {
  background: rgb(var(--v-theme-surface));
}
</style>
