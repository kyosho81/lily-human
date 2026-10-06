<script setup lang="ts">
import { useSpeakingStore } from '@proj-airi/stage-ui/stores/audio'
import { useSpeechOutputControlStore } from '@proj-airi/stage-ui/stores/speech-output-control'
import { storeToRefs } from 'pinia'

// Floating stop-TTS button. LLM replies can be long and TTS reads them for
// minutes; this lets the user cut the voice off immediately. The request goes
// through the shared speech-output-control BroadcastChannel, so whichever tab
// hosts the Stage audio pipeline performs the actual stop.
const { nowSpeaking } = storeToRefs(useSpeakingStore())
const speechOutputControlStore = useSpeechOutputControlStore()

function stop() {
  speechOutputControlStore.requestStopSpeaking('manual-all')
}
</script>

<template>
  <button
    v-show="nowSpeaking"
    class="stop-speaking-btn"
    title="停止语音播报"
    @click="stop"
  >
    ■
  </button>
</template>

<style scoped>
.stop-speaking-btn {
  position: fixed;
  right: 16px;
  bottom: 104px;
  z-index: 9000;
  width: 44px;
  height: 44px;
  border: 0;
  border-radius: 50%;
  background: rgb(190 30 45 / 88%);
  color: #fff;
  font-size: 16px;
  cursor: pointer;
  box-shadow: 0 4px 16px rgb(0 0 0 / 50%);
  animation: ssb-pulse 1.1s ease-in-out infinite alternate;
}

.stop-speaking-btn:hover {
  background: rgb(220 38 38 / 95%);
}

@keyframes ssb-pulse {
  from { transform: scale(1); }
  to { transform: scale(1.12); }
}
</style>