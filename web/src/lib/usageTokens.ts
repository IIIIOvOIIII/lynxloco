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

/**
 * 清到某一天时，那句「日聚合只按天存，故当天更早的记录会被连带删除」是否成立。
 *
 * 日表按 `date >= from_date` 整天删，所以只有当边界那天**真的已经滚进日表**时才谈得上
 * 「连带」。滚存截止是天对齐、且只搬更早的行，日表里的最新日期因此比今天早好几天——
 * 「近 24 小时」那种边界日根本不在表里，那次删除恒命中 0 行。对这一档仍旧无条件说
 * 「会被连带删除」有两个后果：想只清今天的人被吓退，而信了的人清完发现数据还在，
 * 与一个不可逆操作的确认窗自相矛盾。
 *
 * 判据取「日表当前最新日期」这个事实而不是拿保留天数推算：推算要用「今天」，而界面的
 * 今天是浏览器时区、日表的 date 按本机时区写入，两者能差一天。
 *
 * 拿不到 latest（接口没给 / 日表为空）时返回 false——宁可不说，也不说错。
 * 两个参数都是 YYYY-MM-DD，等宽零填充，故可直接字典序比较。
 */
export function dailyCaveatApplies(
  boundaryDate: string | null,
  dailyLatestDate: string | null,
): boolean {
  if (!boundaryDate || !dailyLatestDate) return false;
  return boundaryDate <= dailyLatestDate;
}
