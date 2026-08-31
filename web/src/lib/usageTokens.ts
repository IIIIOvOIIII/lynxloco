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
 * 「连带」。判据卡日表日期区间的**两头**，各挡一类落空：
 *
 * - **上界**（边界日 ≤ 最新日）：滚存截止天对齐、只搬更早的行，日表里的最新日期比今天
 *   早好几天，「近 24 小时」那种边界日根本不在表里，那次删除恒命中 0 行。
 * - **下界**（边界日 ≥ 最早日）：盒子运行天数短于所选范围时（刚装机、刚升级），边界日
 *   早于表里最早的一天，`date >= 边界日` 删掉的都是本就落在所选范围内的整天，不存在
 *   任何超出预期的删除。
 *
 * 说错的代价是双向的：只想清近期的人被吓退，而信了的人清完发现数据还在——一个不可逆
 * 操作的确认窗最不该这样。
 *
 * **残留的不充分性**：区间只保证边界日落在「有数据的那段时间」里，不保证**那一天**有行
 * ——日表按天落行，边界日当天零用量就没有行，此时提示仍会落空。要彻底判准得按天查存在
 * 性（多一次带 from_date 的往返，或把日期集合整个传下来），代价与收益不成比例：区间挡
 * 掉的是「必然落空」的两类，剩下的只在「边界日恰好整天没有用量」时出现。
 *
 * 判据取「日表的日期区间」这个事实，而不是拿保留天数推算：推算要用「今天」，而界面的
 * 今天是浏览器时区、日表的 date 按本机时区写入，两者能差一天。
 *
 * 任一端拿不到（接口没给 / 日表为空）时返回 false——宁可不说，也不说错。
 * 三个参数都是 YYYY-MM-DD，等宽零填充，故可直接字典序比较。
 */
export function dailyCaveatApplies(
  boundaryDate: string | null,
  dailyLatestDate: string | null,
  dailyEarliestDate: string | null = null,
): boolean {
  if (!boundaryDate || !dailyLatestDate || !dailyEarliestDate) return false;
  return boundaryDate >= dailyEarliestDate && boundaryDate <= dailyLatestDate;
}
