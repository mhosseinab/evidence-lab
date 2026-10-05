<script setup lang="ts">
import { nextTick, ref, watch } from "vue";
import { useDashboardContext } from "../composables/dashboardContext";
import { shortId } from "../utils/presentation";

const { state, actions } = useDashboardContext();
const highlighted = ref<number | null>(null);
const cards = new Map<number, HTMLElement>();
watch(
  () => state.evidence,
  () => {
    highlighted.value = null;
  },
);

function setCard(index: number, value: unknown) {
  if (value instanceof HTMLElement) cards.set(index, value);
  else cards.delete(index);
}
async function highlight(index: number) {
  highlighted.value = index;
  await nextTick();
  const card = cards.get(index);
  card?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  card?.focus({ preventScroll: true });
}
defineExpose({ highlight });
</script>

<template>
  <aside class="evidence-column" aria-labelledby="evidence-heading">
    <div class="section-heading">
      <h2 id="evidence-heading">Evidence</h2>
      <span id="evidence-count" class="count-label"
        >{{ state.evidence.length }}
        passage{{ state.evidence.length === 1 ? "" : "s" }}</span
      >
    </div>
    <div id="evidence-list">
      <div v-if="!state.evidence.length" class="evidence-empty">
        <span class="outline-doc" aria-hidden="true">▤</span>
        <h3>The source, right here.</h3>
        <p>Supporting passages will appear when a run retrieves evidence.</p>
      </div>
      <article
        v-for="(item, index) in state.evidence"
        :id="`evidence-${index}`"
        :key="item.id ?? index"
        :ref="value => setCard(index, value)"
        class="evidence-card"
        :class="{ highlight: highlighted === index }"
        tabindex="-1"
      >
        <div class="evidence-card-header">
          <span class="evidence-number">{{ index + 1 }}</span
          ><span class="evidence-title">{{ item.title || item.name || "Source document" }}</span>
        </div>
        <p class="evidence-text">{{ item.text }}</p>
        <div class="evidence-meta">
          <span>Page {{ item.page ?? 1 }} · v{{ shortId(item.version_id) }}</span
          ><button
            v-if="item.version_id"
            class="text-button"
            type="button"
            @click="actions.previewSource(item.version_id, item.title)"
          >
            Open source ↗
          </button>
        </div>
      </article>
    </div>
    <div class="evidence-footnote">
      An answer is checked against the retrieved documents. Source accuracy is a separate question.
    </div>
  </aside>
</template>
