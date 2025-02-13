import unittest
import random
import time

import numpy as np

import torch

SKIP_TEST = None
try:
    #from apex.contrib import xentropy as label_smoothing
    import xentropy_cuda
except ImportError as e:
    SKIP_TEST = e


class SoftmaxCrossEntropyLoss(torch.autograd.Function):
    @staticmethod
    def forward(ctx, logits, labels, smoothing=0.0, padding_idx=-100, half_to_float=False):
        losses, max_log_sum_exp = xentropy_cuda.forward(
            logits, labels, smoothing, half_to_float)
        losses.masked_fill_(labels == padding_idx, 0)

        ctx.save_for_backward(logits, max_log_sum_exp, labels,
                              torch.FloatTensor([smoothing]),
                              torch.LongTensor([padding_idx]))

        return losses

    @staticmethod
    def backward(ctx, grad_loss):
        logits, max_log_sum_exp, labels, smoothing, padding_idx = ctx.saved_tensors

        if not grad_loss.is_contiguous():
            grad_loss = grad_loss.contiguous()
        grad_loss.masked_fill_(labels == padding_idx.item(), 0)
        grad_logits = xentropy_cuda.backward(
            grad_loss.contiguous(), logits, max_log_sum_exp,
            labels, smoothing.item())

        return grad_logits, None, None, None, None


def label_smoothing_raw(x, target, padding_idx, smoothing):
    logprobs = torch.nn.functional.log_softmax(x, dim=-1, dtype=torch.float32)

    non_pad_mask = (target != padding_idx)
    nll_loss = -logprobs.gather(dim=-1, index=target.unsqueeze(1))
    nll_loss = nll_loss.squeeze(1)[non_pad_mask]
    smooth_loss = -logprobs.mean(dim=-1)[non_pad_mask]
    loss = (1.0 - smoothing) * nll_loss + smoothing * smooth_loss
    return loss

def label_smoothing_opt_1(x, target, padding_idx, smoothing):
    logprobs = torch.nn.functional.log_softmax(x, dim=-1, dtype=torch.float32)

    pad_mask = (target == padding_idx)
    ll_loss = logprobs.gather(dim=-1, index=target.unsqueeze(1)).squeeze(1)
    smooth_loss = logprobs.mean(dim=-1)
    loss = (smoothing - 1.0) * ll_loss - smoothing * smooth_loss
    loss.masked_fill_(pad_mask, 0)
    return loss


@unittest.skipIf(SKIP_TEST, f"{SKIP_TEST}")
class LabelSmoothingTest(unittest.TestCase):
    def setUp(self, seed=1234):
        super().setUp()
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)

        # Set pytorch print precision
        torch.set_printoptions(precision=10)

    def gen_test_inputs(self, N, T, H, smoothing, padding_idx, dtype=torch.half):
        logits = torch.randn((N*T, H), dtype=dtype, device='cuda',
            requires_grad=True)
        labels = torch.randint(0, H, [N*T], device='cuda')
        for i in random.sample(range(N*T), N*T//6):
            labels[i] = padding_idx
        half_to_float = (logits.dtype == torch.half)

        return logits, labels, half_to_float

    def print_max_diff_elem(self, ref, tst):
        ref, tst = ref.flatten(), tst.flatten()
        diff = (ref - tst).abs().max()
        idx = (ref - tst).abs().argmax()
        print("Max atol idx: {}, diff: {:.6f}, ref: {:.6f}, tst: {:.6f}".format(
            idx, diff, ref[idx], tst[idx]))

    # def _test_label_smoothing_function(self, dtype):
    #     # Set label smoothing configuration
    #     smoothing, padding_idx = 0.1, 0
    #     N, T, H = 128, 74, 32320
    #     iters = 10
    #     loss_func = SoftmaxCrossEntropyLoss.apply
    #
    #     for i in range(iters):
    #         logits, labels, half_to_float = self.gen_test_inputs(
    #             N, T, H, smoothing, padding_idx)
    #
    #         # Run original softmax cross entropy with label smoothing
    #         logits.grad = None
    #         losses = label_smoothing_raw(logits, labels, padding_idx, smoothing)
    #         loss = losses.sum()
    #         loss.backward()
    #
    #         ref_loss = loss.clone().detach()
    #         ref_grad = logits.grad.clone().detach()
    #
    #         # Run optimized softmax cross entropy with label smoothing
    #         logits.grad = None
    #         losses = loss_func(logits, labels, smoothing, padding_idx, half_to_float)
    #         loss = losses.sum()
    #         loss.backward()
    #
    #         val_loss = loss.clone().detach()
    #         val_grad = logits.grad.clone().detach()
    #
    #         # Validate
    #         self.print_max_diff_elem(ref_grad, val_grad)
    #         torch.testing.assert_close(val_loss, ref_loss)
    #         torch.testing.assert_close(val_grad, ref_grad)

    def _test_label_smoothing_function_pytorch(self, dtype):
        print("[INFO] Testing precision vs Pytorch default function")
        # Set label smoothing configuration
        smoothing, padding_idx = 0.1, 0
        N, T, H = 128, 74, 32320
        iters = 10
        loss_func = SoftmaxCrossEntropyLoss.apply

        loss_func_pytorch = torch.nn.CrossEntropyLoss(ignore_index=padding_idx,
                                                      reduction='sum',
                                                      label_smoothing=smoothing)

        for i in range(iters):
            logits, labels, half_to_float = self.gen_test_inputs(
                N, T, H, smoothing, padding_idx)

            # Run original softmax cross entropy with label smoothing
            logits.grad = None
            losses = loss_func_pytorch(logits.float(), labels)
            loss = losses.sum()
            loss.backward()

            ref_loss = loss.clone().detach()
            ref_grad = logits.grad.clone().detach()

            # Run optimized softmax cross entropy with label smoothing
            logits.grad = None
            losses = loss_func(logits, labels, smoothing, padding_idx, half_to_float)
            loss = losses.sum()
            loss.backward()

            val_loss = loss.clone().detach()
            val_grad = logits.grad.clone().detach()

            # Validate
            self.print_max_diff_elem(ref_grad, val_grad)
            torch.testing.assert_close(val_loss, ref_loss)
            torch.testing.assert_close(val_grad, ref_grad)
    #
    def test_label_smoothing_function_fp16(self):
        self._test_label_smoothing_function_pytorch(torch.half)

    def test_label_smoothing_function_bf16(self):
        self._test_label_smoothing_function_pytorch(torch.bfloat16)
    #
    # def test_label_smoothing_perf(self):
    #     # Set label smoothing configuration
    #     smoothing, padding_idx = 0.1, 0
    #     N, T, H = 128, 74, 32320
    #     iters = 1000
    #     loss_func = SoftmaxCrossEntropyLoss.apply
    #     print()
    #
    #     logits, labels, half_to_float = self.gen_test_inputs(
    #         N, T, H, smoothing, padding_idx)
    #
    #     # Run original softmax cross entropy with label smoothing
    #     torch.cuda.synchronize()
    #     ts = time.time()
    #     for i in range(iters):
    #         logits.grad = None
    #         losses = label_smoothing_raw(logits, labels, padding_idx, smoothing)
    #         loss = losses.sum() / N
    #         loss.backward()
    #     torch.cuda.synchronize()
    #     print("Raw time {:.2f} s elapsed for {} iterations, norm {:.4f}".format(
    #         time.time() - ts, iters, logits.grad.norm()))
    #
    #     # Run optimized softmax cross entropy with label smoothing
    #     torch.cuda.synchronize()
    #     ts = time.time()
    #     for i in range(iters):
    #         logits.grad = None
    #         losses = loss_func(logits, labels, smoothing, padding_idx, half_to_float)
    #         loss = losses.sum() / N
    #         loss.backward()
    #     torch.cuda.synchronize()
    #     print("Opt time {:.2f} s elapsed for {} iterations, norm {:.4f}".format(
    #         time.time() - ts, iters, logits.grad.norm()))
    #
    def test_label_smoothing_perf_pytorch(self):

        print("[INFO] Testing speed vs Pytorch default function")
        # Set label smoothing configuration
        smoothing, padding_idx = 0.1, 0
        N, T, H = 128, 74, 32320
        iters = 1000
        loss_func = SoftmaxCrossEntropyLoss.apply
        print()
        loss_func_pytorch = torch.nn.CrossEntropyLoss(ignore_index=padding_idx,
                                                      reduction='sum',
                                                      label_smoothing=smoothing)

        logits, labels, half_to_float = self.gen_test_inputs(
            N, T, H, smoothing, padding_idx)

        # Run original softmax cross entropy with label smoothing
        torch.cuda.synchronize()
        ts = time.time()
        for i in range(iters):
            logits.grad = None
            losses = loss_func_pytorch(logits, labels)
            loss = losses.sum() / N
            loss.backward()
        torch.cuda.synchronize()
        print("Raw time {:.2f} s elapsed for {} iterations, norm {:.4f}".format(
            time.time() - ts, iters, logits.grad.norm()))

        # Run optimized softmax cross entropy with label smoothing
        torch.cuda.synchronize()
        ts = time.time()
        for i in range(iters):
            logits.grad = None
            losses = loss_func(logits, labels, smoothing, padding_idx, half_to_float)
            loss = losses.sum()  / N
            loss.backward()
        torch.cuda.synchronize()
        print("Opt time {:.2f} s elapsed for {} iterations, norm {:.4f}".format(
            time.time() - ts, iters, logits.grad.norm()))


if __name__ == '__main__':
    unittest.main()