<script setup lang="ts">
defineProps<{ headers: string[]; rows: string[][] }>();
</script>

<template>
  <p v-if="headers.length > 2" class="table-scroll-hint">Scroll to compare all four variants →</p>
  <div
    class="evaluation-metrics"
    :tabindex="headers.length > 2 ? 0 : undefined"
    :role="headers.length > 2 ? 'region' : undefined"
    :aria-label="headers.length > 2 ? 'Variant comparison table, scroll horizontally on a narrow screen' : undefined"
  >
    <table class="metrics-table" :class="{ 'comparison-table': headers.length > 2 }">
      <thead>
        <tr>
          <th v-for="header in headers" :key="header" scope="col">{{ header }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, index) in rows" :key="index">
          <td v-for="(value, cell) in row" :key="cell" class="metrics-value">{{ value }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
