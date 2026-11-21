<script setup lang="ts">
import { onMounted, ref } from "vue";
import TheWelcome from "../components/TheWelcome.vue";

// The page and the API share an origin (httpserver_vite), so this is a same-origin request: no CORS, no proxy
// setting in vite.config.ts, and the same URL in development and when built.
const message = ref("…");
const greetings = ref(0);

async function greet() {
  const response = await fetch("/api/hello");
  const body = (await response.json()) as { message: string; greetings: number };
  message.value = body.message;
  greetings.value = body.greetings;
}

onMounted(greet);
</script>

<template>
  <main>
    <p class="api" data-testid="api">
      {{ message }} <span v-if="greetings">({{ greetings }})</span>
      <button type="button" @click="greet">Again</button>
    </p>
    <TheWelcome />
  </main>
</template>

<style scoped>
.api {
  margin-bottom: 2rem;
}
</style>
