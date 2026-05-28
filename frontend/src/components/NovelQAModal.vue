<!-- AIMETA P=小说AI问答弹窗_项目上下文问答|R=AI问答|NR=不含页面导航|E=component:NovelQAModal|X=ui|A=问答弹窗|D=vue|S=dom,net|RD=./README.ai -->
<template>
  <transition
    enter-active-class="transition-opacity duration-200"
    leave-active-class="transition-opacity duration-200"
    enter-from-class="opacity-0"
    leave-to-class="opacity-0"
  >
    <div v-if="show" class="md-dialog-overlay">
      <div class="absolute inset-0" @click="close"></div>
      <div class="md-dialog qa-dialog relative w-full mx-3 sm:mx-4" @click.stop>
        <div class="md-dialog-header flex items-start justify-between gap-4">
          <div class="min-w-0">
            <h3 class="md-dialog-title">AI 问答</h3>
            <p class="md-body-small truncate" style="color: var(--md-on-surface-variant);">
              {{ projectTitle || '当前小说' }}
            </p>
          </div>
          <button type="button" class="md-icon-btn md-ripple flex-shrink-0" @click="close" aria-label="关闭">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div class="md-dialog-content qa-content">
          <div class="flex flex-wrap gap-2 mb-4">
            <button
              v-for="item in suggestions"
              :key="item"
              type="button"
              class="md-chip md-chip-assist"
              @click="useSuggestion(item)"
            >
              {{ item }}
            </button>
          </div>

          <div class="qa-answer mb-4">
            <p v-if="!answer && !isLoading" class="md-body-medium" style="color: var(--md-on-surface-variant);">
              可以询问这本小说的背景、人物关系、世界观规则、章节线索或后续创作方向。
            </p>
            <div v-else-if="isLoading" class="flex items-center gap-3">
              <div class="md-spinner w-6 h-6"></div>
              <span class="md-body-medium" style="color: var(--md-on-surface-variant);">AI 正在查阅小说资料...</span>
            </div>
            <div v-else class="whitespace-pre-wrap md-body-medium leading-7" style="color: var(--md-on-surface);">
              {{ answer }}
            </div>
          </div>

          <label for="novel-qa-question" class="md-text-field-label">你的问题</label>
          <textarea
            id="novel-qa-question"
            v-model="question"
            rows="4"
            class="md-textarea w-full mt-2"
            maxlength="1000"
            placeholder="例如：这本小说的世界背景是什么？主角和反派的核心冲突是什么？"
            @keydown.ctrl.enter.prevent="ask"
            @keydown.meta.enter.prevent="ask"
          ></textarea>
        </div>

        <div class="md-dialog-actions">
          <button type="button" class="md-btn md-btn-text md-ripple" @click="close">关闭</button>
          <button
            type="button"
            class="md-btn md-btn-filled md-ripple"
            :disabled="isLoading || !question.trim()"
            @click="ask"
          >
            <svg v-if="isLoading" class="w-5 h-5 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            {{ isLoading ? '思考中' : '提问' }}
          </button>
        </div>
      </div>
    </div>
  </transition>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { NovelAPI } from '@/api/novel'

interface Props {
  show: boolean
  projectId: string | null
  projectTitle?: string
}

const props = defineProps<Props>()
const emit = defineEmits<{
  close: []
}>()

const question = ref('')
const answer = ref('')
const isLoading = ref(false)

const suggestions = [
  '这本小说的背景是什么？',
  '主要人物关系是什么？',
  '世界观核心规则是什么？',
  '目前剧情有哪些伏笔？'
]

const close = () => {
  emit('close')
}

const useSuggestion = (value: string) => {
  question.value = value
}

const ask = async () => {
  if (!props.projectId || !question.value.trim() || isLoading.value) return
  isLoading.value = true
  answer.value = ''
  try {
    const response = await NovelAPI.askNovel(props.projectId, question.value.trim())
    answer.value = response.answer
  } catch (error) {
    answer.value = `问答失败：${error instanceof Error ? error.message : '未知错误'}`
  } finally {
    isLoading.value = false
  }
}

watch(
  () => props.show,
  (visible) => {
    if (!visible) return
    answer.value = ''
    isLoading.value = false
  }
)
</script>

<style scoped>
.qa-dialog {
  max-width: min(720px, calc(100vw - 24px));
}

.qa-content {
  max-height: min(68vh, 620px);
  overflow-y: auto;
}

.qa-answer {
  min-height: 8rem;
  padding: 1rem;
  border: 1px solid var(--md-outline-variant);
  border-radius: var(--md-radius-lg);
  background: var(--md-surface-container-low);
}
</style>
