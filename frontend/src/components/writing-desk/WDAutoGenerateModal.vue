<!-- AIMETA P=自动生成章节弹窗_批量生成界面|R=章节自动生成表单|NR=不含生成逻辑|E=component:WDAutoGenerateModal|X=ui|A=生成弹窗|D=vue|S=dom,net|RD=./README.ai -->
<template>
  <TransitionRoot as="template" :show="show">
    <Dialog as="div" class="relative z-50" @close="$emit('close')">
      <TransitionChild as="template" enter="ease-out duration-300" enter-from="opacity-0" enter-to="opacity-100" leave="ease-in duration-200" leave-from="opacity-100" leave-to="opacity-0">
        <div class="fixed inset-0" style="background-color: rgba(0, 0, 0, 0.32);" />
      </TransitionChild>

      <div class="fixed inset-0 z-10 overflow-y-auto">
        <div class="flex min-h-full items-end justify-center p-4 text-center sm:items-center sm:p-0">
          <TransitionChild as="template" enter="ease-out duration-300" enter-from="opacity-0 translate-y-4 sm:translate-y-0 sm:scale-95" enter-to="opacity-100 translate-y-0 sm:scale-100" leave="ease-in duration-200" leave-from="opacity-100 translate-y-0 sm:scale-100" leave-to="opacity-0 translate-y-4 sm:translate-y-0 sm:scale-95">
            <DialogPanel class="md-dialog m3-auto-dialog text-left transition-all sm:my-6 sm:w-full sm:max-w-lg">
              <div class="px-5 pt-6 pb-5 sm:px-6">
                <div class="flex flex-col gap-4 sm:flex-row sm:items-start">
                  <div class="mx-auto flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-full sm:mx-0" style="background-color: var(--md-primary-container);">
                    <svg class="h-6 w-6" style="color: var(--md-on-primary-container);" fill="currentColor" viewBox="0 0 20 20" aria-hidden="true">
                      <path fill-rule="evenodd" d="M4 2a1 1 0 011 1v2.101a7.002 7.002 0 0111.601 2.566 1 1 0 11-1.885.666A5.002 5.002 0 005.999 7H9a1 1 0 010 2H4a1 1 0 01-1-1V3a1 1 0 011-1zm.008 9.057a1 1 0 011.276.61A5.002 5.002 0 0014.001 13H11a1 1 0 110-2h5a1 1 0 011 1v5a1 1 0 11-2 0v-2.101a7.002 7.002 0 01-11.601-2.566 1 1 0 01.61-1.276z" clip-rule="evenodd"></path>
                    </svg>
                  </div>
                  <div class="text-center sm:flex-1 sm:text-left">
                    <DialogTitle as="h3" class="md-headline-small font-semibold leading-7">自动生成章节</DialogTitle>
                    <div class="mt-2">
                      <p class="md-body-medium md-on-surface-variant">
                        从第 {{ startChapter || '-' }} 章开始，最多可生成 {{ maxSelectable }} 章。
                      </p>
                    </div>
                  </div>
                </div>

                <div class="mt-6">
                  <label for="autoNumChapters" class="md-text-field-label">生成数量</label>
                  <input
                    id="autoNumChapters"
                    v-model.number="numChapters"
                    type="number"
                    class="md-text-field-input w-full mt-2"
                    min="1"
                    :max="maxSelectable"
                    :disabled="maxSelectable <= 0"
                  >
                  <div class="mt-5 flex flex-wrap justify-center gap-3">
                    <button
                      v-for="count in quickCounts"
                      :key="count"
                      @click="setNumChapters(count)"
                      :disabled="count > maxSelectable"
                      :class="['md-btn md-btn-outlined md-ripple disabled:opacity-50', numChapters === count ? 'm3-count-selected' : '']"
                    >
                      {{ count }} 章
                    </button>
                  </div>
                </div>
              </div>
              <div class="px-6 py-4 sm:flex sm:flex-row-reverse sm:px-8" style="background-color: var(--md-surface-container-low);">
                <button
                  type="button"
                  class="md-btn md-btn-filled md-ripple sm:ml-3 sm:w-auto w-full justify-center disabled:opacity-50"
                  :disabled="!canGenerate"
                  @click="handleGenerate"
                >
                  开始生成
                </button>
                <button type="button" class="md-btn md-btn-outlined md-ripple sm:mt-0 sm:ml-3 sm:w-auto w-full justify-center mt-3" @click="$emit('close')">取消</button>
              </div>
            </DialogPanel>
          </TransitionChild>
        </div>
      </div>
    </Dialog>
  </TransitionRoot>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Dialog, DialogPanel, DialogTitle, TransitionChild, TransitionRoot } from '@headlessui/vue'

interface Props {
  show: boolean
  maxChapters: number
  startChapter: number | null
}

const props = defineProps<Props>()
const emit = defineEmits(['close', 'generate'])

const numChapters = ref(1)
const maxSelectable = computed(() => Math.max(0, Math.min(50, props.maxChapters || 0)))
const quickCounts = computed(() => [1, 5, 10, 20, 50].filter((count, index, arr) => arr.indexOf(count) === index))
const canGenerate = computed(() => numChapters.value >= 1 && numChapters.value <= maxSelectable.value)

watch(
  () => [props.show, maxSelectable.value],
  () => {
    if (!props.show) return
    numChapters.value = Math.min(Math.max(numChapters.value || 1, 1), maxSelectable.value || 1)
  },
  { immediate: true }
)

const setNumChapters = (count: number) => {
  numChapters.value = Math.min(count, maxSelectable.value || 1)
}

const handleGenerate = () => {
  if (!canGenerate.value) return
  emit('generate', numChapters.value)
  emit('close')
}
</script>

<style scoped>
.m3-auto-dialog {
  border-radius: var(--md-radius-xl);
}

.m3-count-selected {
  background-color: var(--md-primary);
  color: var(--md-on-primary);
  border-color: transparent;
}
</style>
