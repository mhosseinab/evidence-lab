<script setup lang="ts">
import { computed } from "vue";
import { pendingStates, positiveStates, readable, statusLabels, warningStates } from "../utils/presentation";

const props = defineProps<{ status: string }>();
const kind = computed(() =>
  positiveStates.has(props.status)
    ? "positive"
    : warningStates.has(props.status)
      ? "warning"
      : pendingStates.has(props.status)
        ? "processing"
        : "negative",
);
</script>

<template>
  <span class="pill" :class="kind">{{ statusLabels[status] || readable(status) }}</span>
</template>
