/**
 * 用量 token 的口径工具。单独成模块是因为它属于**数据口径**：时间序列分桶与
 * 环形图的模态构成都要用它，两个消费方必须走同一个定义。
 */

/**
 * 「文本」项的口径：input 减掉 video 与 audio 的**残差**，夹到 0。
 *
 * 单独抽出来是因为这条规则是全流程最容易搞错的一处：后端的 input 已经含
 * video + audio（还含图片——未单列该模态），直接把 input 当成「文本」会把
 * video / audio 重复算一遍。分桶与环形图必须走同一个定义。
 *
 * 也因此它不叫「纯文本」：残差里还有图片与系统提示。
 *
 * 夹 0 是防上游偶发不自洽（video + audio > input），不是为了掩盖它。
 */
export function textResidual(input: number, video: number, audio: number): number {
  return Math.max(input - video - audio, 0);
}
