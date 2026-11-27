<template>
  <v-sheet rounded="xl" class="section-card pa-5">
    <div class="d-flex align-center ga-3 mb-4 flex-wrap">
      <v-avatar size="44" rounded="lg" color="primary" variant="tonal" class="section-card__icon">
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
  position: relative;
  background: rgb(var(--v-theme-surface));
  border: 1px solid rgba(var(--v-theme-on-surface), 0.08);
  overflow: hidden;
}
.section-card::before {
  content: "";
  position: absolute;
  left: 0;
  top: 20px;
  bottom: 20px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: rgba(var(--v-theme-primary), 0.5);
}
.section-card__icon {
  background: linear-gradient(135deg, rgba(var(--v-theme-primary), 0.22), rgba(var(--v-theme-primary), 0.08));
}
</style>
