import torch
import unittest
import itertools
from math import inf, nan, isnan
from random import randrange

from torch.testing._internal.common_utils import \
    (TestCase, run_tests, TEST_NUMPY, IS_MACOS, IS_WINDOWS, TEST_WITH_ASAN, make_tensor)
from torch.testing._internal.common_device_type import \
    (instantiate_device_type_tests, dtypes, skipCUDAIfNoMagma, skipCPUIfNoLapack, precisionOverride)
from torch.testing._internal.jit_metaprogramming_utils import gen_script_fn_and_args
from torch.autograd import gradcheck

if TEST_NUMPY:
    import numpy as np

class TestLinalg(TestCase):
    exact_dtype = True

    # Tests torch.outer, and its alias, torch.ger, vs. NumPy
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @precisionOverride({torch.bfloat16: 1e-1})
    @dtypes(*(torch.testing.get_all_dtypes()))
    def test_outer(self, device, dtype):
        def run_test_case(a, b):
            if dtype == torch.bfloat16:
                a_np = a.to(torch.double).cpu().numpy()
                b_np = b.to(torch.double).cpu().numpy()
            else:
                a_np = a.cpu().numpy()
                b_np = b.cpu().numpy()
            expected = np.outer(a_np, b_np)

            self.assertEqual(torch.outer(a, b), expected)
            self.assertEqual(torch.Tensor.outer(a, b), expected)

            self.assertEqual(torch.ger(a, b), expected)
            self.assertEqual(torch.Tensor.ger(a, b), expected)

            # test out variant
            out = torch.empty(a.size(0), b.size(0), device=device, dtype=dtype)
            torch.outer(a, b, out=out)
            self.assertEqual(out, expected)

            out = torch.empty(a.size(0), b.size(0), device=device, dtype=dtype)
            torch.ger(a, b, out=out)
            self.assertEqual(out, expected)

        a = torch.randn(50).to(device=device, dtype=dtype)
        b = torch.randn(50).to(device=device, dtype=dtype)
        run_test_case(a, b)

        # test 0 strided tensor
        zero_strided = torch.randn(1).to(device=device, dtype=dtype).expand(50)
        run_test_case(zero_strided, b)
        run_test_case(a, zero_strided)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @precisionOverride({torch.bfloat16: 1e-1})
    @dtypes(*(torch.testing.get_all_dtypes()))
    def test_addr(self, device, dtype):
        def run_test_case(m, a, b, beta=1, alpha=1):
            if dtype == torch.bfloat16:
                a_np = a.to(torch.double).cpu().numpy()
                b_np = b.to(torch.double).cpu().numpy()
                m_np = m.to(torch.double).cpu().numpy()
            else:
                a_np = a.cpu().numpy()
                b_np = b.cpu().numpy()
                m_np = m.cpu().numpy()

            if beta == 0:
                expected = alpha * np.outer(a_np, b_np)
            else:
                expected = beta * m_np + alpha * np.outer(a_np, b_np)

            self.assertEqual(torch.addr(m, a, b, beta=beta, alpha=alpha), expected)
            self.assertEqual(torch.Tensor.addr(m, a, b, beta=beta, alpha=alpha), expected)

            result_dtype = torch.addr(m, a, b, beta=beta, alpha=alpha).dtype
            out = torch.empty_like(m, dtype=result_dtype)
            torch.addr(m, a, b, beta=beta, alpha=alpha, out=out)
            self.assertEqual(out, expected)

        a = torch.randn(50).to(device=device, dtype=dtype)
        b = torch.randn(50).to(device=device, dtype=dtype)
        m = torch.randn(50, 50).to(device=device, dtype=dtype)

        # when beta is zero
        run_test_case(m, a, b, beta=0., alpha=2)

        # when beta is not zero
        run_test_case(m, a, b, beta=0.5, alpha=2)

        # test transpose
        m_transpose = torch.transpose(m, 0, 1)
        run_test_case(m_transpose, a, b, beta=0.5, alpha=2)

        # test 0 strided tensor
        zero_strided = torch.randn(1).to(device=device, dtype=dtype).expand(50)
        run_test_case(m, zero_strided, b, beta=0.5, alpha=2)

        # test scalar
        m_scalar = torch.tensor(1, device=device, dtype=dtype)
        run_test_case(m_scalar, a, b)

    @dtypes(*itertools.product(torch.testing.get_all_dtypes(),
                               torch.testing.get_all_dtypes()))
    def test_outer_type_promotion(self, device, dtypes):
        a = torch.randn(5).to(device=device, dtype=dtypes[0])
        b = torch.randn(5).to(device=device, dtype=dtypes[1])
        for op in (torch.outer, torch.Tensor.outer, torch.ger, torch.Tensor.ger):
            result = op(a, b)
            self.assertEqual(result.dtype, torch.result_type(a, b))

    @dtypes(*itertools.product(torch.testing.get_all_dtypes(),
                               torch.testing.get_all_dtypes()))
    def test_addr_type_promotion(self, device, dtypes):
        a = torch.randn(5).to(device=device, dtype=dtypes[0])
        b = torch.randn(5).to(device=device, dtype=dtypes[1])
        m = torch.randn(5, 5).to(device=device,
                                 dtype=torch.result_type(a, b))
        for op in (torch.addr, torch.Tensor.addr):
            # pass the integer 1 to the torch.result_type as both
            # the default values of alpha and beta are integers (alpha=1, beta=1)
            desired_dtype = torch.result_type(m, 1)
            result = op(m, a, b)
            self.assertEqual(result.dtype, desired_dtype)

            desired_dtype = torch.result_type(m, 2.)
            result = op(m, a, b, beta=0, alpha=2.)
            self.assertEqual(result.dtype, desired_dtype)

    # Tests migrated from test_torch.py
    # 1) test the shape of the result tensor when there is empty input tensor
    # 2) test the Runtime Exception when there is scalar input tensor
    def test_outer_ger_addr_legacy_tests(self, device):
        for size in ((0, 0), (0, 5), (5, 0)):
            a = torch.rand(size[0], device=device)
            b = torch.rand(size[1], device=device)

            self.assertEqual(torch.outer(a, b).shape, size)
            self.assertEqual(torch.ger(a, b).shape, size)

            m = torch.empty(size, device=device)
            self.assertEqual(torch.addr(m, a, b).shape, size)

        m = torch.randn(5, 6, device=device)
        a = torch.randn(5, device=device)
        b = torch.tensor(6, device=device)
        self.assertRaises(RuntimeError, lambda: torch.outer(a, b))
        self.assertRaises(RuntimeError, lambda: torch.outer(b, a))
        self.assertRaises(RuntimeError, lambda: torch.ger(a, b))
        self.assertRaises(RuntimeError, lambda: torch.ger(b, a))
        self.assertRaises(RuntimeError, lambda: torch.addr(m, a, b))
        self.assertRaises(RuntimeError, lambda: torch.addr(m, b, a))

    # Tests torch.det and its alias, torch.linalg.det, vs. NumPy
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.double)
    def test_det(self, device, dtype):
        tensors = (
            torch.randn((2, 2), device=device, dtype=dtype),
            torch.randn((129, 129), device=device, dtype=dtype),
            torch.randn((3, 52, 52), device=device, dtype=dtype),
            torch.randn((4, 2, 26, 26), device=device, dtype=dtype))


        ops = (torch.det, torch.Tensor.det,
               torch.linalg.det)
        for t in tensors:
            expected = np.linalg.det(t.cpu().numpy())
            for op in ops:
                actual = op(t)
                self.assertEqual(actual, expected)

        # NOTE: det requires a 2D+ tensor
        t = torch.randn(1, device=device, dtype=dtype)
        with self.assertRaises(RuntimeError):
            op(t)

    # This test confirms that torch.linalg.norm's dtype argument works
    # as expected, according to the function's documentation
    @skipCUDAIfNoMagma
    def test_norm_dtype(self, device):
        def run_test_case(input_size, ord, keepdim, from_dtype, to_dtype, compare_dtype):
            msg = (
                f'input_size={input_size}, ord={ord}, keepdim={keepdim}, '
                f'from_dtype={from_dtype}, to_dtype={to_dtype}')
            input = torch.randn(*input_size, dtype=from_dtype, device=device)
            result = torch.linalg.norm(input, ord, keepdim=keepdim, dtype=from_dtype)
            self.assertEqual(result.dtype, from_dtype, msg=msg)
            result_converted = torch.linalg.norm(input, ord, keepdim=keepdim, dtype=to_dtype)
            self.assertEqual(result_converted.dtype, to_dtype, msg=msg)
            self.assertEqual(result.to(compare_dtype), result_converted.to(compare_dtype), msg=msg)

            result_out_converted = torch.empty_like(result_converted)
            torch.linalg.norm(input, ord, keepdim=keepdim, dtype=to_dtype, out=result_out_converted)
            self.assertEqual(result_out_converted.dtype, to_dtype, msg=msg)
            self.assertEqual(result_converted, result_out_converted, msg=msg)

        ord_vector = [0, 1, -1, 2, -2, 3, -3, 4.5, -4.5, inf, -inf, None]
        ord_matrix = [1, -1, 2, -2, inf, -inf, None]
        S = 10
        test_cases = [
            ((S, ), ord_vector),
            ((S, S), ord_matrix),
        ]
        for keepdim in [True, False]:
            for input_size, ord_settings in test_cases:
                for ord in ord_settings:
                    # float to double
                    run_test_case(input_size, ord, keepdim, torch.float, torch.double, torch.float)
                    # double to float
                    run_test_case(input_size, ord, keepdim, torch.double, torch.double, torch.float)

        # Make sure that setting dtype != out.dtype raises an error
        dtype_pairs = [
            (torch.float, torch.double),
            (torch.double, torch.float),
        ]
        for keepdim in [True, False]:
            for input_size, ord_settings in test_cases:
                for ord in ord_settings:
                    for dtype, out_dtype in dtype_pairs:
                        input = torch.rand(*input_size)
                        result = torch.Tensor().to(out_dtype)
                        with self.assertRaisesRegex(RuntimeError, r'provided dtype must match dtype of result'):
                            torch.linalg.norm(input, ord=ord, keepdim=keepdim, dtype=dtype, out=result)

        # TODO: Once dtype arg is supported in nuclear and frobenius norms, remove the following test
        #       and  add 'nuc' and 'fro' to ord_matrix above
        for ord in ['nuc', 'fro']:
            input = torch.randn(10, 10, device=device)
            with self.assertRaisesRegex(RuntimeError, f"ord=\'{ord}\' does not yet support the dtype argument"):
                torch.linalg.norm(input, ord, dtype=torch.float)

    # This test compares torch.linalg.norm and numpy.linalg.norm to ensure that
    # their vector norm results match
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.float, torch.double)
    def test_norm_vector(self, device, dtype):
        def run_test_case(input, p, dim, keepdim):
            result = torch.linalg.norm(input, ord, dim, keepdim)
            input_numpy = input.cpu().numpy()
            result_numpy = np.linalg.norm(input_numpy, ord, dim, keepdim)

            msg = f'input.size()={input.size()}, ord={ord}, dim={dim}, keepdim={keepdim}, dtype={dtype}'
            self.assertEqual(result, result_numpy, msg=msg)

            result_out = torch.empty_like(result)
            torch.linalg.norm(input, ord, dim, keepdim, out=result_out)
            self.assertEqual(result, result_out, msg=msg)

        ord_vector = [0, 1, -1, 2, -2, 3, -3, 4.5, -4.5, inf, -inf, None]
        S = 10
        test_cases = [
            # input size, p settings, dim
            ((S, ), ord_vector, None),
            ((S, ), ord_vector, 0),
            ((S, S, S), ord_vector, 0),
            ((S, S, S), ord_vector, 1),
            ((S, S, S), ord_vector, 2),
            ((S, S, S), ord_vector, -1),
            ((S, S, S), ord_vector, -2),
        ]
        L = 1_000_000
        if dtype == torch.double:
            test_cases.append(((L, ), ord_vector, None))
        for keepdim in [True, False]:
            for input_size, ord_settings, dim in test_cases:
                input = torch.randn(*input_size, dtype=dtype, device=device)
                for ord in ord_settings:
                    run_test_case(input, ord, dim, keepdim)

    # This test compares torch.linalg.norm and numpy.linalg.norm to ensure that
    # their matrix norm results match
    @skipCUDAIfNoMagma
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.float, torch.double)
    def test_norm_matrix(self, device, dtype):
        def run_test_case(input, p, dim, keepdim):
            result = torch.linalg.norm(input, ord, dim, keepdim)
            input_numpy = input.cpu().numpy()
            result_numpy = np.linalg.norm(input_numpy, ord, dim, keepdim)

            msg = f'input.size()={input.size()}, ord={ord}, dim={dim}, keepdim={keepdim}, dtype={dtype}'
            self.assertEqual(result, result_numpy, msg=msg)

            result_out = torch.empty_like(result)
            torch.linalg.norm(input, ord, dim, keepdim, out=result_out)
            self.assertEqual(result, result_out, msg=msg)

        ord_matrix = [1, -1, 2, -2, inf, -inf, 'nuc', 'fro', None]
        S = 10
        test_cases = [
            # input size, p settings, dim
            ((S, S), ord_matrix, None),
            ((S, S), ord_matrix, (0, 1)),
            ((S, S), ord_matrix, (1, 0)),
            ((S, S, S, S), ord_matrix, (2, 0)),
            ((S, S, S, S), ord_matrix, (-1, -2)),
            ((S, S, S, S), ord_matrix, (-1, -3)),
            ((S, S, S, S), ord_matrix, (-3, 2)),
        ]
        L = 1_000
        if dtype == torch.double:
            test_cases.append(((L, L), ord_matrix, None))
        for keepdim in [True, False]:
            for input_size, ord_settings, dim in test_cases:
                input = torch.randn(*input_size, dtype=dtype, device=device)
                for ord in ord_settings:
                    run_test_case(input, ord, dim, keepdim)

    # Test autograd and jit functionality for linalg functions.
    # TODO: Once support for linalg functions is added to method_tests in common_methods_invocations.py,
    #       the `test_cases` entries below should be moved there. These entries are in a similar format,
    #       so they should work with minimal changes.
    @dtypes(torch.float, torch.double)
    def test_autograd_and_jit(self, device, dtype):
        torch.manual_seed(0)
        S = 10
        NO_ARGS = None  # NOTE: refer to common_methods_invocations.py if you need this feature
        test_cases = [
            # NOTE: Not all the features from common_methods_invocations.py are functional here, since this
            #       is only a temporary solution.
            # (
            #   method name,
            #   input size/constructing fn,
            #   args (tuple represents shape of a tensor arg),
            #   test variant name (will be used at test name suffix),    // optional
            #   (should_check_autodiff[bool], nonfusible_nodes, fusible_nodes) for autodiff, // optional
            #   indices for possible dim arg,                            // optional
            #   fn mapping output to part that should be gradcheck'ed,   // optional
            #   kwargs                                                   // optional
            # )
            ('norm', (S,), (), 'default_1d'),
            ('norm', (S, S), (), 'default_2d'),
            ('norm', (S, S, S), (), 'default_3d'),
            ('norm', (S,), (inf,), 'vector_inf'),
            ('norm', (S,), (3.5,), 'vector_3_5'),
            ('norm', (S,), (0.5,), 'vector_0_5'),
            ('norm', (S,), (2,), 'vector_2'),
            ('norm', (S,), (1,), 'vector_1'),
            ('norm', (S,), (0,), 'vector_0'),
            ('norm', (S,), (-inf,), 'vector_neg_inf'),
            ('norm', (S,), (-3.5,), 'vector_neg_3_5'),
            ('norm', (S,), (-0.5,), 'vector_neg_0_5'),
            ('norm', (S,), (2,), 'vector_neg_2'),
            ('norm', (S,), (1,), 'vector_neg_1'),
            ('norm', (S, S), (inf,), 'matrix_inf'),
            ('norm', (S, S), (2,), 'matrix_2', (), NO_ARGS, [skipCPUIfNoLapack, skipCUDAIfNoMagma]),
            ('norm', (S, S), (1,), 'matrix_1'),
            ('norm', (S, S), (-inf,), 'matrix_neg_inf'),
            ('norm', (S, S), (-2,), 'matrix_neg_2', (), NO_ARGS, [skipCPUIfNoLapack, skipCUDAIfNoMagma]),
            ('norm', (S, S), (-1,), 'matrix_neg_1'),
            ('norm', (S, S), ('fro',), 'fro'),
            ('norm', (S, S), ('fro', [0, 1]), 'fro_dim'),
            ('norm', (S, S), ('nuc',), 'nuc', (), NO_ARGS, [skipCPUIfNoLapack, skipCUDAIfNoMagma]),
            ('norm', (S, S), ('nuc', [0, 1]), 'nuc_dim', (), NO_ARGS, [skipCPUIfNoLapack, skipCUDAIfNoMagma]),
        ]
        for test_case in test_cases:
            func_name = test_case[0]
            func = getattr(torch.linalg, func_name)
            input_size = test_case[1]
            args = list(test_case[2])
            test_case_name = test_case[3] if len(test_case) >= 4 else None
            mapping_funcs = list(test_case[6]) if len(test_case) >= 7 else None

            # Skip a test if a decorator tells us to
            if mapping_funcs is not None:
                def decorated_func(self, device, dtype):
                    pass
                for mapping_func in mapping_funcs:
                    decorated_func = mapping_func(decorated_func)
                try:
                    decorated_func(self, device, dtype)
                except unittest.SkipTest:
                    continue

            msg = f'function name: {func_name}, case name: {test_case_name}'

            # Test JIT
            input = torch.randn(*input_size, dtype=dtype, device=device)
            input_script = input.clone().detach()
            script_method, tensors = gen_script_fn_and_args("linalg.norm", "functional", input_script, *args)
            self.assertEqual(
                func(input, *args),
                script_method(input_script),
                msg=msg)

            # Test autograd
            # gradcheck is only designed to work with torch.double inputs
            if dtype == torch.double:
                input = torch.randn(*input_size, dtype=dtype, device=device, requires_grad=True)

                def run_func(input):
                    return func(input, *args)
                self.assertTrue(gradcheck(run_func, input), msg=msg)

    # This test calls torch.linalg.norm and numpy.linalg.norm with illegal arguments
    # to ensure that they both throw errors
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.float, torch.double)
    def test_norm_errors(self, device, dtype):
        def run_error_test_case(input, ord, dim, keepdim, error_type, error_regex):
            test_case_info = (
                f'test case input.size()={input.size()}, ord={ord}, dim={dim}, '
                f'keepdim={keepdim}, dtype={dtype}')

            with self.assertRaisesRegex(error_type, error_regex, msg=test_case_info):
                torch.linalg.norm(input, ord, dim, keepdim)

            input_numpy = input.cpu().numpy()

            msg = f'numpy does not raise error but pytorch does, for case "{test_case_info}"'
            with self.assertRaises(Exception, msg=test_case_info):
                np.linalg.norm(input_numpy, ord, dim, keepdim)

        S = 10
        error_test_cases = [
            # input size, p settings, dim, error type, error regex
            ((S, ), ['fro'], None, RuntimeError, r'order "fro" can only be used if either len\(dim\) == 2'),
            ((S, ), ['nuc'], None, RuntimeError, r'order "nuc" can only be used if either len\(dim\) == 2'),
            ((S, S), [3.5], None, RuntimeError, r'Order 3.5 not supported for matrix norm'),
            ((S, S), [0], None, RuntimeError, r'Order 0 not supported for matrix norm'),
            ((S, S), ['nuc'], 0, RuntimeError, r'order "nuc" can only be used if either len\(dim\) == 2'),
            ((S, S), ['fro'], 0, RuntimeError, r'order "fro" can only be used if either len\(dim\) == 2'),
            ((S, S), ['nuc'], (0, 0), RuntimeError, r'duplicate or invalid dimensions'),
            ((S, S), ['fro', 0], (0, 0), RuntimeError, r'Expected dims to be different'),
            ((S, S), ['fro', 'nuc', 0], (0, 4), IndexError, r'Dimension out of range'),
            ((S, ), [0], (4, ), IndexError, r'Dimension out of range'),
            ((S, ), [None], (0, 0), RuntimeError, r'Expected dims to be different, got this instead'),
            ((S, S, S), [1], (0, 1, 2), RuntimeError, r"'dim' must specify 1 or 2 dimensions"),
            ((S, S, S), [1], None, RuntimeError, r"'dim' must specify 1 or 2 dimensions"),
            ((S, S), ['garbage'], (0, 1), RuntimeError, r'Invalid norm order: garbage'),
        ]
        for keepdim in [True, False]:
            for input_size, ord_settings, dim, error_type, error_regex in error_test_cases:
                input = torch.randn(*input_size, dtype=dtype, device=device)
                for ord in ord_settings:
                    run_error_test_case(input, ord, dim, keepdim, error_type, error_regex)

    # Test complex number inputs for linalg.norm. Some cases are not supported yet, so
    # this test also verifies that those cases raise an error.
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(torch.cfloat, torch.cdouble)
    def test_norm_complex(self, device, dtype):
        def gen_error_message(input_size, ord, keepdim, dim=None):
            return "complex norm failed for input size %s, ord=%s, keepdim=%s, dim=%s" % (
                input_size, ord, keepdim, dim)

        if self.device_type == 'cpu':
            supported_vector_ords = [0, 1, 3, inf, -1, -2, -3, -inf]
            supported_matrix_ords = ['nuc', 1, 2, inf, -1, -2, -inf]
            unsupported_vector_ords = [
                (2, r'norm with p=2 not supported for complex tensors'),
                (None, r'norm with p=2 not supported for complex tensors'),
            ]
            unsupported_matrix_ords = [
                ('fro', r'frobenius norm not supported for complex tensors'),
                (None, r'norm with p=2 not supported for complex tensors'),
            ]

        elif self.device_type == 'cuda':
            supported_vector_ords = [inf, -inf]
            supported_matrix_ords = [1, inf, -1, -inf]
            unsupported_vector_ords = [
                (0, r'norm_cuda" not implemented for \'Complex'),
                (1, r'norm_cuda" not implemented for \'Complex'),
                (2, r'norm with p=2 not supported for complex tensors'),
                (-1, r'norm_cuda" not implemented for \'Complex'),
                (-2, r'norm_cuda" not implemented for \'Complex'),
                (None, r'norm with p=2 not supported for complex tensors'),
            ]
            unsupported_matrix_ords = [
                (None, r'norm with p=2 not supported for complex tensors'),
                ('fro', r'frobenius norm not supported for complex tensors'),
            ]

        # Test supported ords
        for keepdim in [False, True]:
            # vector norm
            x = torch.randn(25, device=device, dtype=dtype)
            xn = x.cpu().numpy()
            for ord in supported_vector_ords:
                res = torch.linalg.norm(x, ord, keepdim=keepdim).cpu()
                expected = np.linalg.norm(xn, ord, keepdims=keepdim)
                msg = gen_error_message(x.size(), ord, keepdim)
                self.assertEqual(res.shape, expected.shape, msg=msg)
                self.assertEqual(res, expected, msg=msg)

            # matrix norm
            x = torch.randn(25, 25, device=device, dtype=dtype)
            xn = x.cpu().numpy()
            for ord in supported_matrix_ords:
                # TODO: Need to fix abort when nuclear norm is given cdouble input:
                #       "double free or corruption (!prev) Aborted (core dumped)"
                if ord == 'nuc' and dtype == torch.cdouble:
                    continue
                res = torch.linalg.norm(x, ord, keepdim=keepdim).cpu()
                expected = np.linalg.norm(xn, ord, keepdims=keepdim)
                msg = gen_error_message(x.size(), ord, keepdim)
                self.assertEqual(res.shape, expected.shape, msg=msg)
                self.assertEqual(res, expected, msg=msg)

        # Test unsupported ords
        # vector norm
        x = torch.randn(25, device=device, dtype=dtype)
        for ord, error_msg in unsupported_vector_ords:
            with self.assertRaisesRegex(RuntimeError, error_msg):
                torch.linalg.norm(x, ord)

        # matrix norm
        x = torch.randn(25, 25, device=device, dtype=dtype)
        for ord, error_msg in unsupported_matrix_ords:
            with self.assertRaisesRegex(RuntimeError, error_msg):
                torch.linalg.norm(x, ord)

    # Test that linal.norm gives the same result as numpy when inputs
    # contain extreme values (inf, -inf, nan)
    @unittest.skipIf(IS_WINDOWS, "Skipped on Windows!")
    @unittest.skipIf(IS_MACOS, "Skipped on MacOS!")
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_norm_extreme_values(self, device):
        vector_ords = [0, 1, 2, 3, inf, -1, -2, -3, -inf]
        matrix_ords = ['fro', 'nuc', 1, 2, inf, -1, -2, -inf]
        vectors = []
        matrices = []
        for pair in itertools.product([inf, -inf, 0.0, nan, 1.0], repeat=2):
            vectors.append(list(pair))
            matrices.append([[pair[0], pair[1]]])
            matrices.append([[pair[0]], [pair[1]]])
        for vector in vectors:
            x = torch.tensor(vector).to(device)
            x_n = x.cpu().numpy()
            for ord in vector_ords:
                msg = f'ord={ord}, vector={vector}'
                result = torch.linalg.norm(x, ord=ord)
                result_n = np.linalg.norm(x_n, ord=ord)
                self.assertEqual(result, result_n, msg=msg)

        # TODO: Remove this function once the broken cases are fixed
        def is_broken_matrix_norm_case(ord, x):
            if self.device_type == 'cuda':
                if x.size() == torch.Size([1, 2]):
                    if ord in ['nuc', 2, -2] and isnan(x[0][0]) and x[0][1] == 1:
                        # These cases are broken because of an issue with svd
                        # https://github.com/pytorch/pytorch/issues/43567
                        return True
            return False

        for matrix in matrices:
            x = torch.tensor(matrix).to(device)
            x_n = x.cpu().numpy()
            for ord in matrix_ords:
                msg = f'ord={ord}, matrix={matrix}'
                result = torch.linalg.norm(x, ord=ord)
                result_n = np.linalg.norm(x_n, ord=ord)

                if is_broken_matrix_norm_case(ord, x):
                    continue
                else:
                    self.assertEqual(result, result_n, msg=msg)

    # Test degenerate shape results match numpy for linalg.norm vector norms
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(TEST_WITH_ASAN, "Skipped on ASAN since it checks for undefined behavior.")
    @dtypes(torch.float, torch.double, torch.cfloat, torch.cdouble)
    def test_norm_vector_degenerate_shapes(self, device, dtype):
        def run_test_case(input, ord, dim, keepdim, should_error):
            msg = f'input.size()={input.size()}, ord={ord}, dim={dim}, keepdim={keepdim}, dtype={dtype}'
            input_numpy = input.cpu().numpy()
            if should_error:
                with self.assertRaises(ValueError):
                    np.linalg.norm(input_numpy, ord, dim, keepdim)
                with self.assertRaises(RuntimeError):
                    torch.linalg.norm(input, ord, dim, keepdim)
            else:
                if dtype in [torch.cfloat, torch.cdouble] and ord in [2, None]:
                    # TODO: Once these ord values have support for complex numbers,
                    #       remove this error test case
                    with self.assertRaises(RuntimeError):
                        torch.linalg.norm(input, ord, dim, keepdim)
                    return
                result_numpy = np.linalg.norm(input_numpy, ord, dim, keepdim)
                result = torch.linalg.norm(input, ord, dim, keepdim)
                self.assertEqual(result, result_numpy, msg=msg)

        ord_vector = [0, 0.5, 1, 2, 3, inf, -0.5, -1, -2, -3, -inf, None]
        S = 10
        test_cases = [
            # input size, p settings that cause error, dim
            ((0, ), [inf, -inf], None),
            ((0, S), [inf, -inf], 0),
            ((0, S), [], 1),
            ((S, 0), [], 0),
            ((S, 0), [inf, -inf], 1),
        ]
        for keepdim in [True, False]:
            for input_size, error_ords, dim in test_cases:
                input = torch.randn(*input_size, dtype=dtype, device=device)
                for ord in ord_vector:
                    run_test_case(input, ord, dim, keepdim, ord in error_ords)

    # Test degenerate shape results match numpy for linalg.norm matrix norms
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(torch.float, torch.double, torch.cfloat, torch.cdouble)
    def test_norm_matrix_degenerate_shapes(self, device, dtype):
        def run_test_case(input, ord, dim, keepdim, should_error):
            if dtype in [torch.cfloat, torch.cdouble] and ord in ['fro', None]:
                # TODO: Once these ord values have support for complex numbers,
                #       remove this error test case
                with self.assertRaises(RuntimeError):
                    torch.linalg.norm(input, ord, dim, keepdim)
                return
            msg = f'input.size()={input.size()}, ord={ord}, dim={dim}, keepdim={keepdim}, dtype={dtype}'
            input_numpy = input.cpu().numpy()
            if should_error:
                with self.assertRaises(ValueError):
                    np.linalg.norm(input_numpy, ord, dim, keepdim)
                with self.assertRaises(RuntimeError):
                    torch.linalg.norm(input, ord, dim, keepdim)
            else:
                result_numpy = np.linalg.norm(input_numpy, ord, dim, keepdim)
                result = torch.linalg.norm(input, ord, dim, keepdim)
                self.assertEqual(result, result_numpy, msg=msg)

        ord_matrix = ['fro', 'nuc', 1, 2, inf, -1, -2, -inf, None]
        S = 10
        test_cases = [
            # input size, p settings that cause error, dim
            ((0, 0), [1, 2, inf, -1, -2, -inf], None),
            ((0, S), [2, inf, -2, -inf], None),
            ((S, 0), [1, 2, -1, -2], None),
            ((S, S, 0), [], (0, 1)),
            ((1, S, 0), [], (0, 1)),
            ((0, 0, S), [1, 2, inf, -1, -2, -inf], (0, 1)),
            ((0, 0, S), [1, 2, inf, -1, -2, -inf], (1, 0)),
        ]
        for keepdim in [True, False]:
            for input_size, error_ords, dim in test_cases:
                input = torch.randn(*input_size, dtype=dtype, device=device)
                for ord in ord_matrix:
                    run_test_case(input, ord, dim, keepdim, ord in error_ords)

    def test_norm_fastpaths(self, device):
        x = torch.randn(3, 5, device=device)

        # slow path
        result = torch.linalg.norm(x, 4.5, 1)
        expected = torch.pow(x.abs().pow(4.5).sum(1), 1.0 / 4.5)
        self.assertEqual(result, expected)

        # fast 0-norm
        result = torch.linalg.norm(x, 0, 1)
        expected = (x != 0).type_as(x).sum(1)
        self.assertEqual(result, expected)

        # fast 1-norm
        result = torch.linalg.norm(x, 1, 1)
        expected = x.abs().sum(1)
        self.assertEqual(result, expected)

        # fast 2-norm
        result = torch.linalg.norm(x, 2, 1)
        expected = torch.sqrt(x.pow(2).sum(1))
        self.assertEqual(result, expected)

        # fast 3-norm
        result = torch.linalg.norm(x, 3, 1)
        expected = torch.pow(x.pow(3).abs().sum(1), 1.0 / 3.0)
        self.assertEqual(result, expected)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_norm_old(self, device):
        def gen_error_message(input_size, p, keepdim, dim=None):
            return "norm failed for input size %s, p=%s, keepdim=%s, dim=%s" % (
                input_size, p, keepdim, dim)

        for keepdim in [False, True]:
            # full reduction
            x = torch.randn(25, device=device)
            xn = x.cpu().numpy()
            for p in [0, 1, 2, 3, 4, inf, -inf, -1, -2, -3, 1.5]:
                res = x.norm(p, keepdim=keepdim).cpu()
                expected = np.linalg.norm(xn, p, keepdims=keepdim)
                self.assertEqual(res, expected, atol=1e-5, rtol=0, msg=gen_error_message(x.size(), p, keepdim))

            # one dimension
            x = torch.randn(25, 25, device=device)
            xn = x.cpu().numpy()
            for p in [0, 1, 2, 3, 4, inf, -inf, -1, -2, -3]:
                dim = 1
                res = x.norm(p, dim, keepdim=keepdim).cpu()
                expected = np.linalg.norm(xn, p, dim, keepdims=keepdim)
                msg = gen_error_message(x.size(), p, keepdim, dim)
                self.assertEqual(res.shape, expected.shape, msg=msg)
                self.assertEqual(res, expected, msg=msg)

            # matrix norm
            for p in ['fro', 'nuc']:
                res = x.norm(p, keepdim=keepdim).cpu()
                expected = np.linalg.norm(xn, p, keepdims=keepdim)
                msg = gen_error_message(x.size(), p, keepdim)
                self.assertEqual(res.shape, expected.shape, msg=msg)
                self.assertEqual(res, expected, msg=msg)

            # zero dimensions
            x = torch.randn((), device=device)
            xn = x.cpu().numpy()
            res = x.norm(keepdim=keepdim).cpu()
            expected = np.linalg.norm(xn, keepdims=keepdim)
            msg = gen_error_message(x.size(), None, keepdim)
            self.assertEqual(res.shape, expected.shape, msg=msg)
            self.assertEqual(res, expected, msg=msg)

            # larger tensor sanity check
            self.assertEqual(
                2 * torch.norm(torch.ones(10000), keepdim=keepdim),
                torch.norm(torch.ones(40000), keepdim=keepdim))

            # matrix norm with non-square >2-D tensors, all combinations of reduction dims
            x = torch.randn(5, 6, 7, 8, device=device)
            xn = x.cpu().numpy()
            for p in ['fro', 'nuc']:
                for dim in itertools.product(*[list(range(4))] * 2):
                    if dim[0] == dim[1]:
                        continue
                    res = x.norm(p=p, dim=dim, keepdim=keepdim).cpu()
                    expected = np.linalg.norm(xn, ord=p, axis=dim, keepdims=keepdim)
                    msg = gen_error_message(x.size(), p, keepdim, dim)
                    self.assertEqual(res.shape, expected.shape, msg=msg)
                    self.assertEqual(res, expected, msg=msg)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_norm_complex_old(self, device):
        def gen_error_message(input_size, p, keepdim, dim=None):
            return "complex norm failed for input size %s, p=%s, keepdim=%s, dim=%s" % (
                input_size, p, keepdim, dim)

        if device == 'cpu':
            for keepdim in [False, True]:
                # vector norm
                x = torch.randn(25, device=device) + 1j * torch.randn(25, device=device)
                xn = x.cpu().numpy()
                for p in [0, 1, 3, inf, -1, -2, -3, -inf]:
                    res = x.norm(p, keepdim=keepdim).cpu()
                    expected = np.linalg.norm(xn, p, keepdims=keepdim)
                    msg = gen_error_message(x.size(), p, keepdim)
                    self.assertEqual(res.shape, expected.shape, msg=msg)
                    self.assertEqual(res, expected, msg=msg)

                # matrix norm
                x = torch.randn(25, 25, device=device) + 1j * torch.randn(25, 25, device=device)
                xn = x.cpu().numpy()
                for p in ['nuc']:
                    res = x.norm(p, keepdim=keepdim).cpu()
                    expected = np.linalg.norm(xn, p, keepdims=keepdim)
                    msg = gen_error_message(x.size(), p, keepdim)
                    self.assertEqual(res.shape, expected.shape, msg=msg)
                    self.assertEqual(res, expected, msg=msg)

            # TODO: remove error test and add functionality test above when 2-norm support is added
            with self.assertRaisesRegex(RuntimeError, r'norm with p=2 not supported for complex tensors'):
                x = torch.randn(2, device=device, dtype=torch.complex64).norm(p=2)

            # TODO: remove error test and add functionality test above when frobenius support is added
            with self.assertRaisesRegex(RuntimeError, r'frobenius norm not supported for complex tensors'):
                x = torch.randn(2, 2, device=device, dtype=torch.complex64).norm(p='fro')

        elif device == 'cuda':
            with self.assertRaisesRegex(RuntimeError, r'"norm_cuda" not implemented for \'ComplexFloat\''):
                (1j * torch.randn(25)).norm()

    # Ensure torch.norm with p='fro' and p=2 give the same results for mutually supported input combinations
    @dtypes(torch.float)
    def test_norm_fro_2_equivalence_old(self, device, dtype):
        input_sizes = [
            (0,),
            (10,),
            (0, 0),
            (4, 30),
            (0, 45),
            (100, 0),
            (45, 10, 23),
            (0, 23, 59),
            (23, 0, 37),
            (34, 58, 0),
            (0, 0, 348),
            (0, 3434, 0),
            (0, 0, 0),
            (5, 3, 8, 1, 3, 5)]

        for input_size in input_sizes:
            a = make_tensor(input_size, device, dtype, low=-9, high=9)

            # Try full reduction
            dim_settings = [None]

            # Try all possible 1-D reductions
            dim_settings += list(range(-a.dim(), a.dim()))

            def wrap_dim(dim, ndims):
                assert (dim < ndims) and (dim >= -ndims)
                if dim >= 0:
                    return dim
                else:
                    return dim + ndims

            # Try all possible 2-D reductions
            dim_settings += [
                (d0, d1) for d0, d1 in itertools.combinations(range(-a.dim(), a.dim()), 2)
                if wrap_dim(d0, a.dim()) != wrap_dim(d1, a.dim())]

            for dim in dim_settings:
                for keepdim in [True, False]:
                    a_norm_2 = torch.norm(a, p=2, dim=dim, keepdim=keepdim)
                    a_norm_fro = torch.norm(a, p='fro', dim=dim, keepdim=keepdim)
                    self.assertEqual(a_norm_fro, a_norm_2)

    @skipCUDAIfNoMagma
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_nuclear_norm_axes_small_brute_force_old(self, device):
        def check_single_nuclear_norm(x, axes):
            if self.device_type != 'cpu' and randrange(100) < 95:
                return  # too many cpu <==> device copies

            a = np.array(x.cpu(), copy=False)
            expected = np.linalg.norm(a, "nuc", axis=axes)

            ans = torch.norm(x, "nuc", dim=axes)
            self.assertTrue(ans.is_contiguous())
            self.assertEqual(ans.shape, expected.shape)
            self.assertEqual(ans.cpu(), expected, rtol=1e-02, atol=1e-03, equal_nan=True)

            out = torch.zeros(expected.shape, dtype=x.dtype, device=x.device)
            ans = torch.norm(x, "nuc", dim=axes, out=out)
            self.assertIs(ans, out)
            self.assertTrue(ans.is_contiguous())
            self.assertEqual(ans.shape, expected.shape)
            self.assertEqual(ans.cpu(), expected, rtol=1e-02, atol=1e-03, equal_nan=True)

        for n in range(1, 3):
            for m in range(1, 3):
                for axes in itertools.permutations([0, 1], 2):
                    # 2d, inner dimensions C
                    x = torch.randn(n, m, device=device)
                    check_single_nuclear_norm(x, axes)

                    # 2d, inner dimensions Fortran
                    x = torch.randn(m, n, device=device).transpose(-1, -2)
                    check_single_nuclear_norm(x, axes)

                    # 2d, inner dimensions non-contiguous
                    x = torch.randn(n, 2 * m, device=device)[:, ::2]
                    check_single_nuclear_norm(x, axes)

                    # 2d, all dimensions non-contiguous
                    x = torch.randn(7 * n, 2 * m, device=device)[::7, ::2]
                    check_single_nuclear_norm(x, axes)

                for o in range(1, 3):
                    for axes in itertools.permutations([0, 1, 2], 2):
                        # 3d, inner dimensions C
                        x = torch.randn(o, n, m, device=device)
                        check_single_nuclear_norm(x, axes)

                        # 3d, inner dimensions Fortran
                        x = torch.randn(o, m, n, device=device).transpose(-1, -2)
                        check_single_nuclear_norm(x, axes)

                        # 3d, inner dimensions non-contiguous
                        x = torch.randn(o, n, 2 * m, device=device)[:, :, ::2]
                        check_single_nuclear_norm(x, axes)

                        # 3d, all dimensions non-contiguous
                        x = torch.randn(7 * o, 5 * n, 2 * m, device=device)[::7, ::5, ::2]
                        check_single_nuclear_norm(x, axes)

                    for r in range(1, 3):
                        for axes in itertools.permutations([0, 1, 2, 3], 2):
                            # 4d, inner dimensions C
                            x = torch.randn(r, o, n, m, device=device)
                            check_single_nuclear_norm(x, axes)

                            # 4d, inner dimensions Fortran
                            x = torch.randn(r, o, n, m, device=device).transpose(-1, -2)
                            check_single_nuclear_norm(x, axes)

                            # 4d, inner dimensions non-contiguous
                            x = torch.randn(r, o, n, 2 * m, device=device)[:, :, :, ::2]
                            check_single_nuclear_norm(x, axes)

                            # 4d, all dimensions non-contiguous
                            x = torch.randn(7 * r, 5 * o, 11 * n, 2 * m, device=device)[::7, ::5, ::11, ::2]
                            check_single_nuclear_norm(x, axes)

    @skipCUDAIfNoMagma
    def test_nuclear_norm_exceptions_old(self, device):
        for lst in [], [1], [1, 2]:
            x = torch.tensor(lst, dtype=torch.double, device=device)
            for axes in (), (0,):
                self.assertRaises(RuntimeError, torch.norm, x, "nuc", axes)
            self.assertRaises(IndexError, torch.norm, x, "nuc", (0, 1))

        x = torch.tensor([[0, 1, 2], [3, 4, 5]], dtype=torch.double, device=device)
        self.assertRaisesRegex(RuntimeError, "duplicate or invalid", torch.norm, x, "nuc", (0, 0))
        self.assertRaisesRegex(IndexError, "Dimension out of range", torch.norm, x, "nuc", (0, 2))


instantiate_device_type_tests(TestLinalg, globals())

if __name__ == '__main__':
    run_tests()
